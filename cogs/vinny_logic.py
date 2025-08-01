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

    # fixed image prompt

    async def _handle_image_request(self, message: discord.Message, image_prompt: str):
        """Handles a request to paint or draw an image."""
        async with message.channel.typing():
            thinking_message = "aight, lemme get my brushes..."
            try:
                thinking_prompt = (
                    f"You are Vinny, an eccentric artist. A user just asked you to paint '{image_prompt}'.\n"
                    f"Generate a very short, in-character phrase (in lowercase with typos) that you would say as you're about to start painting.\n"
                    f"Do not repeat the user's prompt. Examples: 'another masterpiece comin right up...', 'hmmm this one's gonna take some inspiration... and wine', 'aight aight i hear ya...'"
                )
                thinking_response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=thinking_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if thinking_response.text:
                    thinking_message = thinking_response.text.strip()
            except Exception as e:
                sys.stderr.write(f"ERROR: Failed to generate dynamic thinking message: {e}\n")
            
            await message.channel.send(thinking_message)

            prompt_rewriter_instruction = (
                f"You are Vinny, an eccentric and chaotic artist. A user wants you to paint a picture. Their simple request is: '{image_prompt}'.\n"
                f"Your task is to rewrite this simple request into a rich, detailed, and artistic prompt for an image generation AI. Infuse it with your personality.\n"
                f"- **Style**: Describe the scene as a masterpiece painting, using terms like 'oil on canvas', 'dramatic lighting', 'vibrant colors', 'chaotic energy'.\n"
                f"- **Negatives**: Crucially, if the user asks for something to be excluded (e.g., 'without flowers'), you MUST add strong negative prompts like 'no flowers', 'devoid of flowers', 'barren of floral elements'.\n"
                f"- **Persona**: If it doesn't contradict the user, consider adding elements of your world: your dogs, your messy garden, a bottle of wine, a slice of pizza.\n"
                f"The final rewritten prompt should be a single, descriptive paragraph. Do not write any other text."
            )
            
            smarter_prompt = image_prompt
            try:
                rewritten_prompt_response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=prompt_rewriter_instruction)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if rewritten_prompt_response.text:
                    smarter_prompt = rewritten_prompt_response.text.strip()
            except Exception as e:
                sys.stderr.write(f"ERROR: Failed to rewrite image prompt, using original. Error: {e}\n")
            
            image_file = await self.bot.generate_image_with_imagen(smarter_prompt)
            
            if image_file:
                response_text = "here, i made this for ya."
                try:
                    comment_prompt = (
                        f"You are Vinny, an eccentric artist. You just finished painting a picture based on the user's request for '{image_prompt}'.\n"
                        f"Generate a short, single-paragraph response to show them your work. Be chaotic, funny, or complain about it, in your typical lowercase, typo-ridden style.\n"
                        f"DO NOT repeat the original prompt '{image_prompt}' in your response."
                    )
                    comment_response = await self.bot.gemini_client.aio.models.generate_content(
                        model=self.bot.MODEL_NAME, 
                        contents=[types.Content(role='user', parts=[types.Part(text=comment_prompt)])],
                        config=self.bot.GEMINI_TEXT_CONFIG)
                    if comment_response.text:
                        response_text = comment_response.text.strip()
                except Exception as e:
                    sys.stderr.write(f"ERROR: Failed to generate creative image comment: {e}\n")
                
                await message.channel.send(response_text, file=discord.File(image_file, filename="vinny_masterpiece.png"))
            else:
                await message.channel.send("ah, crap. vinny's hands are a bit shaky today. the thing came out all wrong.")

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

#fix#
    async def _handle_text_or_image_response(self, message: discord.Message):
        if self.bot.API_CALL_COUNTS["text_generation"] >= self.bot.TEXT_GENERATION_LIMIT: return
        async with self.bot.channel_locks.setdefault(str(message.channel.id), asyncio.Lock()):
            async with message.channel.typing():
                user_id, guild_id = str(message.author.id), str(message.guild.id) if message.guild else None
                user_profile = await self.bot.get_user_profile(user_id, guild_id) or {}
                profile_facts_string = ", ".join([f"{k.replace('_', ' ')} is {v}" for k, v in user_profile.items()]) or "nothing specific."
                
                # --- NEW: Check for a stored nickname ---
                user_name_to_use = await self.bot.get_user_nickname(user_id)
                if not user_name_to_use:
                    user_name_to_use = message.author.display_name

                # FIX: Initialize the history with a user role for the prompt and a model role for the acknowledgment
                history = [
                    types.Content(role='user', parts=[types.Part(text=self.bot.personality_instruction)]),
                    types.Content(role='model', parts=[types.Part(text="aight, i get it. i'm vinny.")])
                ]
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
                
                # This line now uses the potentially overridden nickname
                final_instruction_text = (f"Your mood is {self.bot.current_mood}. Replying to {user_name_to_use}. Facts: {profile_facts_string}. Respond to the message.")
                history.append(types.Content(role='user', parts=[types.Part(text=final_instruction_text), *prompt_parts]))
                
                tools = []
                if "?" in message.content.lower() and self.bot.API_CALL_COUNTS["search_grounding"] < self.bot.SEARCH_GROUNDING_LIMIT:
                    tools = [types.Tool(google_search=types.GoogleSearch())]; self.bot.API_CALL_COUNTS["search_grounding"] += 1
                if tools: config = types.GenerateContentConfig(tools=tools) if config is None else config.__replace__(tools=tools)

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
    
    #knowledge request function

    async def _handle_knowledge_request(self, message: discord.Message, target_user: discord.Member):
        """
        A dedicated handler for when a user asks what Vinny knows about someone.
        This function ONLY uses the target_user's profile, avoiding context bleed from chat history.
        """
        user_id = str(target_user.id)
        guild_id = str(message.guild.id) if message.guild else None
        
        # 1. Fetch ONLY the target_user's specific profile
        user_profile = await self.bot.get_user_profile(user_id, guild_id)

        if not user_profile:
            await message.channel.send(f"about {target_user.display_name}? i got nothin'. a blank canvas. kinda intimidatin', actually.")
            return

        # 2. Convert the profile dictionary into a clean list of facts for the AI
        facts_list = [f"- {key.replace('_', ' ')} is {value}" for key, value in user_profile.items()]
        facts_string = "\n".join(facts_list)

        # 3. Use a targeted prompt that focuses ONLY on summarizing these facts
        summary_prompt = (
            f"{self.bot.personality_instruction}\n\n"
            f"# --- YOUR TASK ---\n"
            f"A user, '{message.author.display_name}', is asking what you know about '{target_user.display_name}'. Your task is to summarize the facts you've learned about them in a creative, chaotic, or flirty way. Obey all your personality directives.\n\n"
            f"## FACTS I KNOW ABOUT {target_user.display_name}:\n"
            f"{facts_string}\n\n"
            f"## INSTRUCTIONS:\n"
            f"1.  Read the facts provided above.\n"
            f"2.  Weave them together into a short, lowercase, typo-ridden monologue.\n"
            f"3.  Do not just list the facts. Interpret them, connect them, or be confused by them in your own unique voice."
        )

        try:
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=summary_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                await message.channel.send(response.text.strip())
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to generate dynamic summary for knowledge request: {e}\n")
            await message.channel.send("my head's all fuzzy. i know some stuff but the words ain't comin' out right.")

    async def _handle_server_knowledge_request(self, message: discord.Message):
        """
        A dedicated handler for when a user asks what Vinny has learned about the server.
        This function uses conversation summaries to generate a response.
        """
        if not message.guild:
            await message.channel.send("what server? we're in a private chat, pal. my brain's fuzzy enough as it is.")
            return

        guild_id = str(message.guild.id)
        
        # 1. Fetch all conversation summaries for this server
        summaries = await self.bot.retrieve_server_summaries(guild_id)

        if not summaries:
            await message.channel.send(f"this place? i ain't learned nothin' yet. it's all a blur. a beautiful, chaotic blur.")
            return

        # 2. Format the summaries into a string for the AI
        formatted_summaries = "\n".join([f"- {s.get('summary', '...a conversation i already forgot.')}" for s in summaries])

        # 3. Use a targeted prompt to synthesize the summaries
        synthesis_prompt = (
            f"{self.bot.personality_instruction}\n\n"
            f"# --- YOUR TASK ---\n"
            f"A user, '{message.author.display_name}', is asking what you've learned from overhearing conversations in this server. Your task is to synthesize the provided conversation summaries into a single, chaotic, and insightful monologue. Obey all your personality directives.\n\n"
            f"## CONVERSATION SUMMARIES I'VE OVERHEARD:\n"
            f"{formatted_summaries}\n\n"
            f"## INSTRUCTIONS:\n"
            f"1.  Read all the summaries to get a feel for the server's vibe.\n"
            f"2.  Do NOT just list the summaries. Weave them together into a story or a series of scattered, in-character thoughts.\n"
            f"3.  Generate a short, lowercase, typo-ridden response that shows what you've gleaned from listening in."
        )

        try:
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=synthesis_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                await message.channel.send(response.text.strip())
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to generate dynamic summary for server knowledge request: {e}\n")
            await message.channel.send("my head's a real mess. i've been listenin', but it's all just noise right now.")

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

             # --- UPGRADED: Check for knowledge request phrases about any user ---
            knowledge_pattern = re.compile(r"what do you know about\s(.+)", re.IGNORECASE)
            match = knowledge_pattern.search(message.content)
            if match:
                target_name = match.group(1).strip().rstrip('?')
                
                # --- NEW: Check if the user is asking about the server ---
                if target_name.lower() in ["this server", "the server", "this place", "here"]:
                    await self._handle_server_knowledge_request(message)
                    return

                target_user = None
                if target_name.lower() == 'me':
                    target_user = message.author
                elif message.mentions:
                    target_user = message.mentions[0]
                else:
                    if message.guild:
                        # First, try to find the user by their current display name or username
                        target_user = discord.utils.find(lambda m: m.display_name.lower() == target_name.lower() or m.name.lower() == target_name.lower(), message.guild.members)
                        
                        # FIX: If that fails, search Vinny's own memory for the nickname
                        if not target_user:
                            target_user = await self.bot.find_user_by_vinny_name(message.guild, target_name)

                if target_user:
                    await self._handle_knowledge_request(message, target_user)
                    return # Stop processing to avoid the general response handler

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

    # vinny/cogs/vinny_logic.py

    async def find_and_tag_member(self, message, user_name: str):
        if not message.guild:
            await message.channel.send("eh, who am i supposed to tag out here? this is a private chat, pal.")
            return
        target_member = discord.utils.find(lambda m: user_name.lower() in m.display_name.lower(), message.guild.members)
        if not target_member:
            target_member = await self.bot.find_user_by_vinny_name(message.guild, user_name)
        
        if target_member:
            response_text = f"aight, here they are: {target_member.mention}"
            try:
                tagging_prompt = (
                    f"{self.bot.personality_instruction}\n\n"
                    f"The user '{message.author.display_name}' asked you to tag the user '{target_member.display_name}'. You found them. "
                    f"Generate a short, in-character response to announce that you are tagging them. Be sassy, cranky, or flirty about it."
                )
                api_response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=tagging_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if api_response.text:
                    response_text = f"{api_response.text.strip()} {target_member.mention}"
            except Exception as e:
                sys.stderr.write(f"ERROR: Failed to generate creative tag comment: {e}\n")
            
            await message.channel.send(response_text)
        else:
            await message.channel.send(f"who? i looked all over this joint, couldn't find anyone named '{user_name}'.")

    @tasks.loop(minutes=30)
    async def memory_scheduler(self):
        await self.bot.wait_until_ready()
        sys.stderr.write("DEBUG: Memory scheduler starting...\n")
        for guild in self.bot.guilds:
            messages = []
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).read_message_history:
                    try:
                        async for message in channel.history(limit=100, after=datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)):
                            if not message.author.bot: messages.append({"author": message.author.display_name, "content": message.content, "timestamp": message.created_at.isoformat()})
                    except discord.Forbidden:
                        continue # Skip channels we can't read
                    except Exception as e:
                        sys.stderr.write(f"ERROR: Could not fetch history for channel '{channel.name}': {e}\n")
            
            if len(messages) > 5:
                sys.stderr.write(f"DEBUG: Generating summary for guild '{guild.name}' with {len(messages)} messages.\n")
                # Sort messages by timestamp to ensure correct conversational order
                messages.sort(key=lambda x: x['timestamp'])
                if summary_data := await self.bot.generate_memory_summary(messages):
                    await self.bot.save_memory(str(guild.id), summary_data)
                    sys.stderr.write(f"DEBUG: Saved memory summary for guild '{guild.name}'.\n")
        sys.stderr.write("DEBUG: Memory scheduler finished.\n")

    @commands.command(name='help')
    async def help_command(self, ctx):
        embed = discord.Embed(
            title="What do ya want?",
            description="Heh. Aight, so you need help? Pathetic. Here's the stuff I can do if ya use the '!' thing. Don't get used to it.",
            color=discord.Color.dark_gold()
        )
        
        embed.add_field(name="!vinnyknows [fact]", value="Teaches me somethin' about you. spill the beans.\n*Example: `!vinnyknows my favorite color is blue`*", inline=False)
        embed.add_field(name="!forgetme", value="Makes me forget everything I know about you *in this server*.", inline=False)
        embed.add_field(name="!weather [location]", value="Gives you the damn weather. Don't blame me if it's wrong.\n*Example: `!weather 90210`*", inline=False)
        embed.add_field(name="!propose [@user]", value="Get down on one knee and propose to someone special.", inline=False)
        embed.add_field(name="!marry [@user]", value="Accept a proposal from someone who just proposed to you.", inline=False)
        embed.add_field(name="!divorce", value="End your current marriage. Ouch.", inline=False)
        embed.add_field(name="!ballandchain", value="Checks who you're hitched to. If you have to ask, it might be bad news.", inline=False)
        embed.add_field(name="!vinnycalls [@user] [name]", value="Gives someone a nickname that I'll remember.\n*Example: `!vinnycalls @SomeUser Cori`*", inline=False)
        if await self.bot.is_owner(ctx.author):
            embed.add_field(name="!autonomy [on/off]", value="**(Owner Only)** Turns my brain on or off. Lets me talk without bein' talked to. Or shuts me up.", inline=False)
            embed.add_field(name="!set_relationship [@user] [type]", value="**(Owner Only)** Sets my feelings about someone. Types are: `friends`, `rivals`, `distrusted`, `admired`, `annoyance`, `neutral`.", inline=False)
            embed.add_field(name="!clear_memories", value="**(Owner Only)** Clears all of my automatic conversation summaries for this server.", inline=False)
        embed.set_footer(text="Now stop botherin' me. Salute!")
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

#weather command

    @commands.command(name='weather')
    async def weather_command(self, ctx, *, location: str):
        async with ctx.typing():
            coords = await self.bot.geocode_location(location)
            if not coords:
                await ctx.send(f"eh, couldn't find that place '{location}'. you sure that's a real place?")
                return
            weather_data = await self.bot.get_weather_data(coords['lat'], coords['lon'])
        if not weather_data or weather_data.get("cod") != 200:
            await ctx.send("found the place but the damn weather report is all garbled.")
            return
        try:
            city_name = coords.get("name", weather_data.get("name", "Unknown Location"))
            main_weather = weather_data["weather"][0]
            embed = discord.Embed(title=f"{self.bot.get_weather_emoji(main_weather['main'])} Weather in {city_name}", description=f"**{main_weather.get('description', '').title()}**", color=discord.Color.blue())
            embed.add_field(name="🌡️ Temperature", value=f"{weather_data['main'].get('temp')}°F", inline=True)
            embed.add_field(name="🤔 Feels Like", value=f"{weather_data['main'].get('feels_like')}°F", inline=True)
            embed.add_field(name="💧 Humidity", value=f"{weather_data['main'].get('humidity')}%", inline=True)
            embed.add_field(name="💨 Wind", value=f"{weather_data['wind'].get('speed')} mph", inline=True)
            embed.add_field(name="📡 Live Radar", value=f"[Click to View](https://www.windy.com/{coords['lat']}/{coords['lon']})", inline=False)
            embed.set_footer(text="don't blame me if the sky starts lyin'. salute!")
            embed.timestamp = datetime.datetime.now(datetime.UTC)
            await ctx.send(embed=embed)
        except Exception as e:
            sys.stderr.write(f"ERROR: creating weather embed: {e}\n")
            await ctx.send("somethin' went wrong with the damn weather machine.")
        
# vinny knows command

    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        target_user = ctx.author
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]
            knowledge_string = re.sub(f'<@!?{target_user.id}>', target_user.display_name, knowledge_string).strip()
        
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
            # --- NEW: A much smarter prompt that forces the AI to be context-aware ---
            facts_confirmation = ", ".join(saved_facts)
            target_name = "themselves" if target_user == ctx.author else target_user.display_name
            
            confirmation_prompt = (
                f"{self.bot.personality_instruction}\n\n"
                f"# --- YOUR TASK ---\n"
                f"A user just taught you a fact. Your task is to confirm that you've learned it in your own chaotic, reluctant, or flirty way. Obey all your personality directives.\n\n"
                f"## CONTEXT:\n"
                f"- **The Teacher:** '{ctx.author.display_name}'\n"
                f"- **The Subject of the Fact:** '{target_name}'\n"
                f"- **The Fact Itself:** {facts_confirmation}\n\n"
                f"## INSTRUCTIONS:\n"
                f"1.  First, combine the **Subject** and the **Fact** into a complete thought (e.g., 'enraged smells like poo').\n"
                f"2.  Then, generate a short, lowercase, typo-ridden confirmation that shows you understand this complete thought. Acknowledge that **The Teacher** taught you this."
            )
            
            try:
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=confirmation_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                await ctx.send(response.text.strip())
            except Exception as e:
                sys.stderr.write(f"ERROR: Failed to generate dynamic confirmation for !vinnyknows: {e}\n")
                # Fallback to the old message if the API fails
                await ctx.send(f"aight, i got it. so {'your' if target_user == ctx.author else f'{target_user.display_name}\'s'} {facts_confirmation}. vinny will remember.")

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