import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import random
import re
import json
import sys
import io
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Coroutine

from google.genai import types
from PIL import Image

# This block is only processed by type-checkers, not when the code runs.
if TYPE_CHECKING:
    from main import VinnyBot, extract_facts_from_message

# We need to import the function from main
from main import extract_facts_from_message

class VinnyLogic(commands.Cog):
    def __init__(self, bot: 'VinnyBot'):
        self.bot = bot
        self.memory_scheduler.start()

    def cog_unload(self):
        self.memory_scheduler.cancel()
    
    # ... (all helper functions like _handle_image_request, _handle_reply, etc. are unchanged)
    # The only change is to the vinnyknows_command and help_command
    async def _handle_image_request(self, message: discord.Message, image_prompt: str):
        async with message.channel.typing():
            thinking_message = "aight, lemme get my brushes..."
            try:
                thinking_prompt = (f"You are Vinny... generate a short phrase... '{image_prompt}'...")
                thinking_response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[types.Content(parts=[types.Part(text=thinking_prompt)])], config=self.bot.GEMINI_TEXT_CONFIG)
                if thinking_response.text: thinking_message = thinking_response.text.strip()
            except Exception as e: pass
            await message.channel.send(thinking_message)
            prompt_rewriter_instruction = (f"You are Vinny... rewrite this prompt... '{image_prompt}'...")
            smarter_prompt = image_prompt
            try:
                rewritten_prompt_response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[types.Content(parts=[types.Part(text=prompt_rewriter_instruction)])], config=self.bot.GEMINI_TEXT_CONFIG)
                if rewritten_prompt_response.text: smarter_prompt = rewritten_prompt_response.text.strip()
            except Exception as e: pass
            image_file = await self.bot.generate_image_with_imagen(smarter_prompt)
            if image_file:
                response_text = "here, i made this for ya."
                try:
                    comment_prompt = (f"You are Vinny... comment on the image for '{image_prompt}'...")
                    comment_response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[types.Content(parts=[types.Part(text=comment_prompt)])], config=self.bot.GEMINI_TEXT_CONFIG)
                    if comment_response.text: response_text = comment_response.text.strip()
                except Exception as e: pass
                await message.channel.send(response_text, file=discord.File(image_file, filename="vinny_masterpiece.png"))
            else: await message.channel.send("ah, crap. vinny's hands are shaky.")

    async def _handle_reply(self, message: discord.Message):
        try:
            replied_to_message = await message.channel.fetch_message(message.reference.message_id)
            prompt_parts = []
            config = self.bot.GEMINI_TEXT_CONFIG
            if replied_to_message.attachments and "image" in replied_to_message.attachments[0].content_type:
                attachment = replied_to_message.attachments[0]
                image_bytes = await attachment.read()
                prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=image_bytes)))
                prompt_parts.append(types.Part(text=(f"User '{message.author.display_name}' replied with: \"{message.content}\" to an older message with an image.")))
                config = None
            else:
                prompt_parts.append(types.Part(text=(f"User '{message.author.display_name}' replied with: \"{message.content}\" to an older message which said: \"{replied_to_message.content}\".")))
            
            dynamic_persona_injection = f"current mood is '{self.bot.current_mood}'."
            final_reply_prompt_parts = [types.Part(text=f"{self.bot.personality_instruction}\n{dynamic_persona_injection}"), *prompt_parts]
            
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[types.Content(parts=final_reply_prompt_parts)], config=config)
                if response.text:
                    for chunk in self.bot.split_message(response.text):
                        await message.channel.send(chunk.lower())
        except Exception as e: pass

    async def _handle_text_or_image_response(self, message: discord.Message):
        if self.bot.API_CALL_COUNTS["text_generation"] >= self.bot.TEXT_GENERATION_LIMIT: return
        async with self.bot.channel_locks.setdefault(str(message.channel.id), asyncio.Lock()):
            async with message.channel.typing():
                user_id, guild_id = str(message.author.id), str(message.guild.id) if message.guild else None
                user_profile = await self.bot.get_user_profile(user_id, guild_id) or {}
                profile_facts_string = ", ".join([f"{k.replace('_', ' ')} is {v}" for k, v in user_profile.items()]) or "nothing specific."
                
                history = [types.Content(parts=[types.Part(text=self.bot.personality_instruction)])]
                async for msg in message.channel.history(limit=self.bot.MAX_CHAT_HISTORY_LENGTH):
                    if msg.id == message.id: continue
                    history.append(types.Content(role="model" if msg.author == self.bot.user else "user", parts=[types.Part(text=f"{msg.author.display_name}: {msg.content}")]))
                history.reverse()
                
                prompt_parts = [types.Part(text=message.content)]
                config = self.bot.GEMINI_TEXT_CONFIG
                if message.attachments:
                    for attachment in message.attachments:
                        if "image" in attachment.content_type:
                            prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=await attachment.read())))
                            config = None; break
                
                final_instruction_text = (f"Your mood is {self.bot.current_mood}. Replying to {message.author.display_name}. Facts: {profile_facts_string}. Respond to the message.")
                history.append(types.Content(role='user', parts=[types.Part(text=final_instruction_text), *prompt_parts]))
                
                tools = []
                if "?" in message.content.lower() and self.bot.API_CALL_COUNTS["search_grounding"] < self.bot.SEARCH_GROUNDING_LIMIT:
                    tools = [types.Tool(google_search=types.GoogleSearch())]; self.bot.API_CALL_COUNTS["search_grounding"] += 1
                if tools: config = types.GenerateContentConfig(tools=tools) if config is None else config._replace(tools=tools)

                self.bot.API_CALL_COUNTS["text_generation"] += 1; await self.bot.update_api_count_in_firestore()
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)
                
                while response.candidates and response.candidates[0].content.parts and response.candidates[0].content.parts[0].function_call:
                    function_call = response.candidates[0].content.parts[0].function_call
                    tool_response = self.bot.gemini_client.tools.google_search(function_call.args)
                    history.append(response.candidates[0].content)
                    history.append(types.Content(parts=[types.Part(function_response=types.FunctionResponse(name='Google Search', response={'result': tool_response}))]))
                    response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)
                
                if response.text and response.text.strip().lower() != '[silence]':
                    for chunk in self.bot.split_message(response.text): await message.channel.send(chunk.lower())
                
                if self.bot.PASSIVE_LEARNING_ENABLED and not message.attachments:
                    if extracted_facts := await extract_facts_from_message(self.bot, message.content):
                        for key, value in extracted_facts.items(): await self.bot.save_user_profile_fact(user_id, guild_id, key, value)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.id in self.bot.processed_message_ids or message.content.startswith(self.bot.command_prefix): return
        self.bot.processed_message_ids[message.id] = True
        try:
            should_respond, is_direct_reply = False, False
            if message.reference:
                if (await message.channel.fetch_message(message.reference.message_id)).author == self.bot.user:
                    should_respond, is_direct_reply = True, True
            if not should_respond:
                bot_names = ["vinny", "vincenzo", "vin vin"]
                if self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in bot_names): should_respond = True
                elif self.bot.autonomous_mode_enabled and message.guild and random.random() < self.bot.autonomous_reply_chance: should_respond = True
                elif message.guild is None: should_respond = True
            
            if not should_respond:
                if "pie" in message.content.lower() and random.random() < 0.75: await message.add_reaction('🥧')
                elif random.random() < self.bot.reaction_chance: await message.add_reaction(random.choice(message.guild.emojis) if message.guild.emojis else '👍')
                return
            
            await self.update_vinny_mood()
            if is_direct_reply: return await self._handle_reply(message)

            cleaned_actions = message.content.lower().replace(f'<@!{self.bot.user.id}>', '').strip()
            if any(cleaned_actions.startswith(kw) for kw in ["paint", "draw"]): return await self._handle_image_request(message, cleaned_actions)
            if any(cleaned_actions.startswith(kw) for kw in ["tag", "ping"]): return await self.find_and_tag_member(message, cleaned_actions)
            if "what's my name" in message.content.lower():
                name = await self.bot.get_user_nickname(str(message.author.id))
                return await message.channel.send(f"they call ya {name}, right?" if name else "i got nothin'.")
            if match := re.search(r"my name is\s+([A-Z][a-z]{2,})", message.content, re.IGNORECASE):
                if await self.bot.save_user_nickname(str(message.author.id), match.group(1)): return await message.channel.send(f"aight, {match.group(1)}, got it.")
            
            await self._handle_text_or_image_response(message)
        except Exception as e:
            sys.stderr.write(f"CRITICAL ERROR in on_message: {e}\n")
            import traceback; traceback.print_exc()

    async def update_vinny_mood(self):
        if datetime.datetime.now() - self.bot.last_mood_change_time > self.bot.MOOD_CHANGE_INTERVAL:
            self.bot.current_mood = random.choice([m for m in self.bot.MOODS if m != self.bot.current_mood])
            self.bot.last_mood_change_time = datetime.datetime.now()

    async def find_and_tag_member(self, message, user_name: str):
        if not message.guild: return
        target_member = discord.utils.find(lambda m: user_name.lower() in m.display_name.lower(), message.guild.members)
        if not target_member: target_member = await self.bot.find_user_by_vinny_name(message.guild, user_name)
        if target_member: await message.channel.send(f"aight, here they are: {target_member.mention}")
        else: await message.channel.send(f"who? couldn't find anyone named '{user_name}'.")

    @tasks.loop(minutes=30)
    async def memory_scheduler(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            messages = []
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).read_message_history:
                    async for message in channel.history(limit=100, after=datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)):
                        if not message.author.bot: messages.append({"author": message.author.display_name, "content": message.content})
            if len(messages) > 5:
                if summary_data := await self.bot.generate_memory_summary(messages):
                    await self.bot.save_memory(str(guild.id), summary_data)

    @commands.command(name='help')
    async def help_command(self, ctx):
        embed = discord.Embed(title="What do ya want?", description="Here's the stuff I can do.", color=discord.Color.dark_gold())
        embed.add_field(name="!vinnyknows [fact]", value="Teaches me somethin' about you.", inline=False)
        embed.add_field(name="!forgetme", value="Makes me forget what I know about you in this server.", inline=False)
        # ... Add other commands
        await ctx.send(embed=embed)

    @commands.command(name='vinnycalls')
    async def vinnycalls_command(self, ctx, member: discord.Member, nickname: str):
        if await self.bot.save_user_nickname(str(member.id), nickname):
            await ctx.send(f"aight, got it. callin' {member.mention} '{nickname}'.")

    @commands.command(name='forgetme')
    async def forgetme_command(self, ctx):
        if not ctx.guild: return await ctx.send("this only works in a server, pal.")
        if await self.bot.delete_user_profile(str(ctx.author.id), str(ctx.guild.id)):
            await ctx.send(f"aight, {ctx.author.mention}. i scrambled my brains. who are you again?")
        else: await ctx.send("my head's already empty, pal.")

    @commands.command(name='propose')
    async def propose_command(self, ctx, member: discord.Member):
        if ctx.author == member: return await ctx.send("proposin' to yourself? i get it.")
        if member.bot: return await ctx.send("proposin' to a robot? find a real pulse.")
        if await self.bot.save_proposal(str(ctx.author.id), str(member.id)):
            await ctx.send(f"whoa! {ctx.author.mention} is on one knee for {member.mention}. you got 5 mins to say yes with `!marry @{ctx.author.display_name}`.")

    @commands.command(name='marry')
    async def marry_command(self, ctx, member: discord.Member):
        if not await self.bot.check_proposal(str(member.id), str(ctx.author.id)):
            return await ctx.send(f"{member.display_name} didn't propose to you.")
        if await self.bot.finalize_marriage(str(member.id), str(ctx.author.id)):
            await ctx.send(f":tada: they said yes! i now pronounce {member.mention} and {ctx.author.mention} hitched!")

    @commands.command(name='divorce')
    async def divorce_command(self, ctx):
        profile = await self.bot.get_user_profile(str(ctx.author.id), None)
        if not profile or "married_to" not in profile: return await ctx.send("you ain't married.")
        partner_id = profile["married_to"]
        if await self.bot.process_divorce(str(ctx.author.id), partner_id):
            await ctx.send(f"it's over. {ctx.author.mention} has split from <@{partner_id}>. 📜")

    @commands.command(name='ballandchain')
    async def ballandchain_command(self, ctx):
        profile = await self.bot.get_user_profile(str(ctx.author.id), None)
        if profile and profile.get("married_to"):
            partner_id = profile.get("married_to")
            partner_name = (await self.bot.fetch_user(partner_id)).display_name
            await ctx.send(f"you're shackled to **{partner_name}**. happened on **{profile.get('marriage_date')}**.")
        else: await ctx.send("you ain't married to nobody.")

    @commands.command(name='weather')
    async def weather_command(self, ctx, *, location: str):
        async with ctx.typing():
            coords = await self.bot.geocode_location(location)
            if not coords: return await ctx.send(f"couldn't find '{location}'.")
            weather = await self.bot.get_weather_data(coords['lat'], coords['lon'])
        if not weather: return await ctx.send("weather report is garbled.")
        embed = discord.Embed(title=f"{self.bot.get_weather_emoji(weather['weather'][0]['main'])} Weather in {coords['name']}", color=discord.Color.blue())
        embed.add_field(name="🌡️ Temp", value=f"{weather['main']['temp']}°F")
        await ctx.send(embed=embed)

    @commands.command(name='remember')
    async def remember_command(self, ctx, *, text: str):
        if await self.bot.save_explicit_memory(str(ctx.author.id), str(ctx.guild.id) if ctx.guild else None, text):
            await ctx.send(f"alright, {ctx.author.mention}. vinny's got it.")

    @commands.command(name='recall')
    async def recall_command(self, ctx, *, topic: str):
        await ctx.send(await self.bot.retrieve_explicit_memories(str(ctx.author.id), str(ctx.guild.id) if ctx.guild else None, topic))
        
    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        target_user = ctx.author
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]
            knowledge_string = re.sub(f'<@!?{target_user.id}>', target_user.display_name, knowledge_string).strip()
        
        # --- FIX: Call the standalone function and add better logging ---
        extracted_facts = await extract_facts_from_message(self.bot, knowledge_string)
        
        if not extracted_facts:
            sys.stderr.write(f"DEBUG: Fact extraction failed for string: '{knowledge_string}'\n")
            await ctx.send("eh? what're you tryin' to tell me? i didn't get that. try sayin' it like 'my favorite food is pizza'.")
            return

        saved_facts = []
        for key, value in extracted_facts.items():
            if await self.bot.save_user_profile_fact(str(target_user.id), str(ctx.guild.id) if ctx.guild else None, key, value):
                saved_facts.append(f"'{key}' is '{value}'")

        if saved_facts:
            await ctx.send(f"aight, i got it. so {'your' if target_user == ctx.author else f'{target_user.display_name}\'s'} {', '.join(saved_facts)}. vinny will remember.")
        else:
            await ctx.send("my head's all fuzzy. tried to remember that.")

    @commands.command(name='autonomy')
    @commands.is_owner()
    async def autonomy_command(self, ctx, status: str):
        if status.lower() == 'on': self.bot.autonomous_mode_enabled = True; await ctx.send("aight, vinny's off the leash.")
        elif status.lower() == 'off': self.bot.autonomous_mode_enabled = False; await ctx.send("thank god. vinny's back in his cage.")

    @commands.command(name='set_relationship')
    @commands.is_owner()
    async def set_relationship_command(self, ctx, member: discord.Member, rel_type: str):
        if rel_type.lower() in ['friends', 'rivals', 'distrusted', 'admired', 'annoyance', 'neutral']:
            if await self.bot.save_user_profile_fact(str(member.id), str(ctx.guild.id), 'relationship_status', rel_type.lower()):
                await ctx.send(f"aight, got it. me and {member.display_name} are... '{rel_type.lower()}'.")
        else: await ctx.send("that ain't a real relationship type.")

    @commands.command(name='clear_memories')
    @commands.is_owner()
    async def clear_memories_command(self, ctx):
        if not ctx.guild: return
        if await self.bot.delete_docs_from_firestore(self.bot.db.collection(f"artifacts/{self.bot.APP_ID}/servers/{str(ctx.guild.id)}/summaries")):
            await ctx.send("aight, it's done. all the old chatter is gone.")

async def setup(bot):
    await bot.add_cog(VinnyLogic(bot))