import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import random
import re
import json
import logging
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Coroutine

from google.genai import types

# Import from our new utility modules
from utils import constants, api_clients

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

    # --- Listener for All Commands' Errors ---
    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """A global error handler for all commands in this cog."""
        
        # Log every error to the console for debugging
        logging.error(f"Error in command '{ctx.command}':", exc_info=error)

        if isinstance(error, commands.CommandNotFound):
            return  # Let the on_message event handle this for natural language interaction
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"eh, you missed somethin'. you need to provide the '{error.param.name}'.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(f"who? couldn't find anyone named '{error.argument}'.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("you're not the boss of me, but also i literally can't do that. check my permissions.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"whoa there, slow down. try again in {error.retry_after:.2f} seconds.")
        elif isinstance(error, commands.is_owner):
            await ctx.send("heh. nice try, pal.")
        else:
            # For any other error, give a generic, in-character message
            await ctx.send("ah crap, my brain just shorted out. somethin' went wrong with that command.")

    # --- Core Message Handling ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.id in self.bot.processed_message_ids or message.content.startswith(self.bot.command_prefix):
            return
        
        self.bot.processed_message_ids[message.id] = True
        
        try:
            # --- Correction Handling ---
            correction_keywords = ["i'm not", "i am not", "i don't", "i do not", "that's not true", "that isn't true"]
            is_mention = self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in ["vinny", "vincenzo"])
            if is_mention and any(keyword in message.content.lower() for keyword in correction_keywords):
                return await self._handle_correction(message)

            # --- Image Reply Handling ---
            if message.reference and self.bot.user.mentioned_in(message):
                original_message = await message.channel.fetch_message(message.reference.message_id)
                if original_message.attachments and "image" in original_message.attachments[0].content_type:
                    return await self._handle_image_reply(message, original_message)
            
            # --- Determine if Bot Should Respond ---
            should_respond, is_direct_reply, is_autonomous = False, False, False
            if message.reference:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg.author == self.bot.user:
                    should_respond, is_direct_reply = True, True
            
            if not should_respond:
                bot_names = ["vinny", "vincenzo", "vin vin"]
                if self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in bot_names):
                    should_respond = True
                elif self.bot.autonomous_mode_enabled and message.guild and random.random() < self.bot.autonomous_reply_chance:
                    should_respond, is_autonomous = True, True
                elif message.guild is None: # DM Channel
                    should_respond = True
            
            if not should_respond:
                # --- Passive Reactions ---
                explicit_reaction_keywords = ["react to this", "add an emoji", "emoji this", "react vinny"]
                if "pie" in message.content.lower() and random.random() < 0.75:
                    await message.add_reaction('🥧')
                elif any(keyword in message.content.lower() for keyword in explicit_reaction_keywords) or (random.random() < self.bot.reaction_chance):
                    try:
                        emoji = random.choice(message.guild.emojis) if message.guild and message.guild.emojis else random.choice(['😂', '👍', '👀', '🍕', '🍻', '🥃', '🐶', '🎨'])
                        await message.add_reaction(emoji)
                    except discord.Forbidden:
                        logging.warning(f"Missing permissions to add reactions in {message.channel.id}")
                    except Exception as e:
                        logging.warning(f"Failed to add reaction: {e}")
                return

            # --- Active Response Logic ---
            await self.update_vinny_mood()
            if is_direct_reply:
                return await self._handle_reply(message)

            # --- Natural Language Command Parsing ---
            knowledge_pattern = re.compile(r"what do you know about\s(.+)", re.IGNORECASE)
            if match := knowledge_pattern.search(message.content):
                target_name = match.group(1).strip().rstrip('?')
                if target_name.lower() in ["this server", "the server", "this place", "here"]:
                    return await self._handle_server_knowledge_request(message)
                
                target_user = None
                if target_name.lower() == 'me':
                    target_user = message.author
                elif message.mentions:
                    target_user = message.mentions[0]
                elif message.guild:
                    target_user = discord.utils.find(lambda m: m.display_name.lower() == target_name.lower() or m.name.lower() == target_name.lower(), message.guild.members)
                    if not target_user:
                        target_user = await self._find_user_by_vinny_name(message.guild, target_name)
                
                if target_user:
                    return await self._handle_knowledge_request(message, target_user)

            cleaned_actions = re.sub(f'<@!?{self.bot.user.id}>', '', message.content, flags=re.IGNORECASE).strip()
            bot_names = ["vinny", "vincenzo", "vin vin"]
            for name in bot_names:
                if cleaned_actions.lower().startswith(f"{name} "):
                    cleaned_actions = cleaned_actions[len(name)+1:]
                    break
            
            image_trigger_keywords = ["paint", "draw", "make a picture of", "create an image of", "generate an image of"]
            if any(cleaned_actions.lower().startswith(kw) for kw in image_trigger_keywords):
                prompt_text = cleaned_actions
                for kw in image_trigger_keywords:
                    if cleaned_actions.lower().startswith(kw):
                        prompt_text = cleaned_actions[len(kw):].strip()
                        break
                return await self._handle_image_request(message, prompt_text)
            
            tag_keywords = ["tag", "ping"]
            for keyword in tag_keywords:
                if cleaned_actions.lower().startswith(keyword + " "):
                    times_to_tag = 1
                    if match := re.search(r'(\d+)\s+times', cleaned_actions, re.IGNORECASE):
                        try:
                            times_to_tag = int(match.group(1))
                        except ValueError:
                            times_to_tag = 1
                    
                    name_to_find = message.mentions[0].display_name if message.mentions else cleaned_actions[len(keyword)+1:].strip().split(' ')[0]
                    return await self.find_and_tag_member(message, name_to_find, times_to_tag)

            if "what's my name" in message.content.lower():
                name = await self.bot.firestore_service.get_user_nickname(str(message.author.id))
                return await message.channel.send(f"they call ya {name}, right?" if name else "i got nothin'.")
            
            if match := re.search(r"my name is\s+([A-Z][a-z]{2,})", message.content, re.IGNORECASE):
                if await self.bot.firestore_service.save_user_nickname(str(message.author.id), match.group(1)):
                    return await message.channel.send(f"aight, {match.group(1)}, got it.")
            
            await self._handle_text_or_image_response(message, is_autonomous=is_autonomous)

        except Exception:
            logging.critical("CRITICAL ERROR in on_message", exc_info=True)

    # --- Internal Helper for Finding Users by Nickname ---
    async def _find_user_by_vinny_name(self, guild: discord.Guild, target_name: str):
        if not self.bot.firestore_service or not guild:
            return None
        for member in guild.members:
            nickname = await self.bot.firestore_service.get_user_nickname(str(member.id))
            if nickname and nickname.lower() == target_name.lower():
                return member
        return None

    # --- Handlers (Called by on_message) ---
    async def _handle_image_request(self, message: discord.Message, image_prompt: str):
        async with message.channel.typing():
            thinking_message = "aight, lemme get my brushes..."
            try:
                thinking_prompt = (
                    f"You are Vinny, an eccentric artist. A user just asked you to paint '{image_prompt}'.\n"
                    f"Generate a very short, in-character phrase (in lowercase with typos) that you would say as you're about to start painting.\n"
                    f"Do not repeat the user's prompt. Examples: 'another masterpiece comin right up...', 'hmmm this one's gonna take some inspiration... and rum', 'aight aight i hear ya...'"
                )
                thinking_response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=thinking_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if thinking_response.text:
                    thinking_message = thinking_response.text.strip()
            except Exception as e:
                logging.warning(f"Failed to generate dynamic thinking message: {e}")
            
            await message.channel.send(thinking_message)

            prompt_rewriter_instruction = (
                f"You are Vinny, an eccentric and chaotic artist. A user wants you to paint a picture. Their simple request is: '{image_prompt}'.\n"
                f"Your task is to rewrite this request into a richer, more detailed, and artistic prompt for an image generation AI. Infuse it with your personality while respecting the user's original vision.\n"
                f"## RULES:\n"
                f"1.  **Preserve the Core Subject**: This is your most important rule. The final prompt MUST be about the user's original subject. For example, if they ask for 'a cat', the final prompt must be about a cat. Do not change the subject.\n"
                f"2.  **Enhance with Style**: Describe the scene as a masterpiece painting. Use artistic terms like 'oil on canvas', 'dramatic lighting', 'vibrant colors', 'chaotic energy'. This is how you add your flair.\n"
                f"3.  **Respect Negatives**: If the user asks for something to be excluded (e.g., 'no hats'), you MUST add strong negative prompts like 'no hats, wearing no headwear, bare-headed'.\n"
                f"4.  **Persona is Secondary**: You can ONLY add elements of your own world (your dogs, rum, pizza) if they DO NOT contradict or overshadow the user's original request. If the user's request is very specific, do not add your own elements.\n\n"
                f"The final rewritten prompt should be a single, descriptive paragraph focused on enhancing the user's idea. Do not write any other text."
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
                logging.warning(f"Failed to rewrite image prompt, using original.", exc_info=True)
            
            image_file = await api_clients.generate_image_with_imagen(
                self.bot.http_session, self.bot.loop, smarter_prompt, self.bot.GCP_PROJECT_ID, self.bot.FIREBASE_B64
            )
            
            if image_file:
                response_text = "here, i made this for ya."
                try:
                    image_file.seek(0)
                    image_bytes = image_file.read()
                    comment_prompt_text = (
                        f"You are Vinny, an eccentric artist. You just finished painting the attached picture based on the user's request for '{image_prompt}'.\n"
                        f"Your task is to generate a short, single-paragraph response to show them your work. LOOK AT THE IMAGE and comment on what you ACTUALLY painted. "
                        f"Be chaotic, funny, or complain about it in your typical lowercase, typo-ridden style."
                    )
                    prompt_parts = [
                        types.Part(text=comment_prompt_text),
                        types.Part(inline_data=types.Blob(mime_type="image/png", data=image_bytes))
                    ]
                    comment_response = await self.bot.gemini_client.aio.models.generate_content(
                        model=self.bot.MODEL_NAME,
                        contents=[types.Content(role='user', parts=prompt_parts)],
                        config=self.bot.GEMINI_TEXT_CONFIG
                    )
                    if comment_response.text:
                        response_text = comment_response.text.strip()
                except Exception:
                    logging.error("Failed to generate creative image comment.", exc_info=True)
                
                image_file.seek(0)
                await message.channel.send(response_text, file=discord.File(image_file, filename="vinny_masterpiece.png"))
            else:
                await message.channel.send("ah, crap. vinny's hands are a bit shaky today. the thing came out all wrong.")

    async def _handle_reply(self, message: discord.Message):
        try:
            user_id = str(message.author.id)
            user_name_to_use = await self.bot.firestore_service.get_user_nickname(user_id) or message.author.display_name

            replied_to_message = await message.channel.fetch_message(message.reference.message_id)
            prompt_parts = []
            config = self.bot.GEMINI_TEXT_CONFIG

            if replied_to_message.attachments and "image" in replied_to_message.attachments[0].content_type:
                attachment = replied_to_message.attachments[0]
                image_bytes = await attachment.read()
                prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=image_bytes)))
                prompt_parts.append(types.Part(text=(f"User '{user_name_to_use}' replied with: \"{message.content}\" to an older message with an image.")))
                config = None
            else:
                prompt_parts.append(types.Part(text=(f"User '{user_name_to_use}' replied with: \"{message.content}\" to an older message which said: \"{replied_to_message.content}\".")))
            
            dynamic_persona_injection = f"current mood is '{self.bot.current_mood}'."
            final_reply_prompt_parts = [types.Part(text=f"{self.bot.personality_instruction}\n{dynamic_persona_injection}"), *prompt_parts]
            
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME, 
                    contents=[types.Content(parts=final_reply_prompt_parts)], 
                    config=config
                )
                if response.text:
                    for chunk in self.bot.split_message(response.text):
                        await message.channel.send(chunk.lower())
        except Exception:
            logging.error("Failed to handle a direct reply.", exc_info=True)

    async def _handle_image_reply(self, reply_message: discord.Message, original_message: discord.Message):
        try:
            image_attachment = original_message.attachments[0]
            image_bytes = await image_attachment.read()
            user_comment = reply_message.content

            prompt_text = (
                f"{self.bot.personality_instruction}\n\n"
                f"# --- YOUR TASK ---\n"
                f"A user, '{reply_message.author.display_name}', just replied to the attached image with the comment: \"{user_comment}\".\n"
                f"Your task is to look at the image and respond to their comment in your unique, chaotic, and flirty voice. Obey all personality directives."
            )
            prompt_parts = [
                types.Part(text=prompt_text),
                types.Part(inline_data=types.Blob(mime_type=image_attachment.content_type, data=image_bytes))
            ]

            async with reply_message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=prompt_parts)],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if response.text:
                    for chunk in self.bot.split_message(response.text):
                        await reply_message.channel.send(chunk.lower())

        except Exception:
            logging.error("Failed to handle an image reply.", exc_info=True)
            await reply_message.channel.send("my eyes are all blurry, couldn't make out the picture, pal.")

    async def _handle_text_or_image_response(self, message: discord.Message, is_autonomous: bool = False):
        if self.bot.API_CALL_COUNTS["text_generation"] >= self.bot.TEXT_GENERATION_LIMIT:
            logging.warning("Text generation limit reached. Skipping response.")
            return

        async with self.bot.channel_locks.setdefault(str(message.channel.id), asyncio.Lock()):
            async with message.channel.typing():
                user_id, guild_id = str(message.author.id), str(message.guild.id) if message.guild else None
                user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id) or {}
                profile_facts_string = ", ".join([f"{k.replace('_', ' ')} is {v}" for k, v in user_profile.items()]) or "nothing specific."
                
                user_name_to_use = await self.bot.firestore_service.get_user_nickname(user_id) or message.author.display_name

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
                
                final_instruction_text = (f"Your mood is {self.bot.current_mood}. You are autonomously chiming in on a conversation. Comment on the last message, which was from '{user_name_to_use}'. Your known facts about them are: {profile_facts_string}.") if is_autonomous else (f"Your mood is {self.bot.current_mood}. Replying to {user_name_to_use}. Facts: {profile_facts_string}. Respond to the message.")
                history.append(types.Content(role='user', parts=[types.Part(text=final_instruction_text), *prompt_parts]))
                
                tools = []
                if "?" in message.content.lower() and self.bot.API_CALL_COUNTS["search_grounding"] < self.bot.SEARCH_GROUNDING_LIMIT:
                    tools = [types.Tool(google_search=types.GoogleSearch())]
                    self.bot.API_CALL_COUNTS["search_grounding"] += 1
                
                if tools:
                    config = types.GenerateContentConfig(tools=tools) if config is None else config.__replace__(tools=tools)

                self.bot.API_CALL_COUNTS["text_generation"] += 1
                await self.bot.update_api_count_in_firestore()
                
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)
                
                while response.candidates and response.candidates[0].content.parts and response.candidates[0].content.parts[0].function_call:
                    function_call = response.candidates[0].content.parts[0].function_call
                    tool_response_part = self.bot.gemini_client.tools.google_search.call(function_call)
                    history.append(response.candidates[0].content)
                    history.append(types.Content(parts=[tool_response_part]))
                    response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)
                
                if response.text:
                    cleaned_response = response.text.strip()
                    if cleaned_response and cleaned_response.lower() != '[silence]':
                        for chunk in self.bot.split_message(cleaned_response):
                            await message.channel.send(chunk.lower())
                
                if self.bot.PASSIVE_LEARNING_ENABLED and not message.attachments:
                    if extracted_facts := await extract_facts_from_message(self.bot, message.content):
                        for key, value in extracted_facts.items():
                            await self.bot.firestore_service.save_user_profile_fact(user_id, guild_id, key, value)

    async def _handle_knowledge_request(self, message: discord.Message, target_user: discord.Member):
        user_id = str(target_user.id)
        guild_id = str(message.guild.id) if message.guild else None
        user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)

        if not user_profile:
            await message.channel.send(f"about {target_user.display_name}? i got nothin'. a blank canvas. kinda intimidatin', actually.")
            return

        facts_list = [f"- {key.replace('_', ' ')} is {value}" for key, value in user_profile.items()]
        facts_string = "\n".join(facts_list)
        summary_prompt = (
            f"{self.bot.personality_instruction}\n\n"
            f"# --- YOUR TASK ---\n"
            f"You are being asked what you know about '{target_user.display_name}'. Your only task is to summarize the facts listed below about them in a creative, chaotic, or flirty way. Do not mention any other user. Obey all your personality directives.\n\n"
            f"## FACTS I KNOW ABOUT {target_user.display_name}:\n"
            f"{facts_string}\n\n"
            f"## INSTRUCTIONS:\n"
            f"1.  Read ONLY the facts provided above about {target_user.display_name}.\n"
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
        except Exception:
            logging.error("Failed to generate knowledge summary.", exc_info=True)
            await message.channel.send("my head's all fuzzy. i know some stuff but the words ain't comin' out right.")

    async def _handle_server_knowledge_request(self, message: discord.Message):
        if not message.guild:
            await message.channel.send("what server? we're in a private chat, pal. my brain's fuzzy enough as it is.")
            return

        guild_id = str(message.guild.id)
        summaries = await self.bot.firestore_service.retrieve_server_summaries(guild_id)

        if not summaries:
            await message.channel.send(f"this place? i ain't learned nothin' yet. it's all a blur. a beautiful, chaotic blur.")
            return

        formatted_summaries = "\n".join([f"- {s.get('summary', '...a conversation i already forgot.')}" for s in summaries])
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
        except Exception:
            logging.error("Failed to generate server knowledge summary.", exc_info=True)
            await message.channel.send("my head's a real mess. i've been listenin', but it's all just noise right now.")

    async def _handle_correction(self, message: discord.Message):
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else None
        
        correction_prompt = (
            f"A user is correcting a fact about themselves. Their message is: \"{message.content}\".\n"
            f"Your task is to identify the specific fact they are correcting. For example, if they say 'I'm not bald', the fact is 'is bald'. If they say 'I don't have a cat', the fact is 'has a cat'.\n"
            f"Please return a JSON object with a single key, \"fact_to_remove\", containing the fact you identified.\n\n"
            f"Example:\n"
            f"User message: 'Vinny, that's not true, my favorite color is red, not blue.'\n"
            f"Output: {{\"fact_to_remove\": \"favorite color is blue\"}}"
        )
        try:
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME, contents=[types.Part(text=correction_prompt)], config=self.bot.GEMINI_TEXT_CONFIG
                )
                json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if not json_match:
                    await message.channel.send("my brain's all fuzzy, i didn't get what i was wrong about.")
                    return

                fact_data = json.loads(json_match.group(0))
                fact_to_remove = fact_data.get("fact_to_remove")
                if not fact_to_remove:
                    await message.channel.send("huh? what was i wrong about? try bein more specific, pal.")
                    return

                user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
                if not user_profile:
                    await message.channel.send("i don't even know anything about you to be wrong about!")
                    return

                key_mapping_prompt = (
                    f"A user's profile is stored as a JSON object. I need to find the key that corresponds to the fact: \"{fact_to_remove}\".\n"
                    f"Here is the user's current profile data: {json.dumps(user_profile, indent=2)}\n"
                    f"Based on the data, which key is the most likely match for the fact I need to remove? Return a JSON object with a single key, \"database_key\".\n\n"
                    f"Example:\n"
                    f"Fact: 'is a painter'\n"
                    f"Profile: {{\"occupation\": \"a painter\"}}\n"
                    f"Output: {{\"database_key\": \"occupation\"}}"
                )
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME, contents=[types.Part(text=key_mapping_prompt)], config=self.bot.GEMINI_TEXT_CONFIG
                )
                json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if not json_match: return

                key_data = json.loads(json_match.group(0))
                db_key = key_data.get("database_key")
                if not db_key or db_key not in user_profile:
                    await message.channel.send("i thought i knew somethin' but i can't find it in my brain. weird.")
                    return

                if await self.bot.firestore_service.delete_user_profile_fact(user_id, guild_id, db_key):
                    await message.channel.send(f"aight, my mistake. i'll forget that whole '{db_key.replace('_', ' ')}' thing. salute.")
                else:
                    await message.channel.send("i tried to forget it, but the memory is stuck in there good. damn.")
        except Exception:
            logging.error("An error occurred in _handle_correction.", exc_info=True)
            await message.channel.send("my head's poundin'. somethin went wrong tryin to fix my memory.")

    # --- Mood & Memory Tasks ---
    async def update_vinny_mood(self):
        if datetime.datetime.now() - self.bot.last_mood_change_time > self.bot.MOOD_CHANGE_INTERVAL:
            self.bot.current_mood = random.choice([m for m in self.bot.MOODS if m != self.bot.current_mood])
            self.bot.last_mood_change_time = datetime.datetime.now()
            logging.info(f"Vinny's mood has changed to: {self.bot.current_mood}")

    async def _generate_memory_summary(self, messages):
        if not messages or not self.bot.firestore_service.db: return None
        summary_instruction = ("You are a conversation summarization assistant. Analyze the following conversation and provide a concise, one-paragraph summary. "
                               "After the summary, provide a list of 3-5 relevant keywords. Your output must contain 'summary:' and 'keywords:' labels.")
        summary_prompt = f"{summary_instruction}\n\n...conversation:\n" + "\n".join([f"{msg['author']}: {msg['content']}" for msg in messages])
        
        try:
            response = await self.bot.gemini_client.aio.models.generate_content(
                model=self.bot.MODEL_NAME, 
                contents=[types.Content(parts=[types.Part(text=summary_prompt)])], 
                config=self.bot.GEMINI_TEXT_CONFIG
            )

            if response.text:
                # Use regex to robustly parse the output
                summary_match = re.search(r"summary:\s*(.*?)(keywords:|$)", response.text, re.DOTALL | re.IGNORECASE)
                keywords_match = re.search(r"keywords:\s*(.*)", response.text, re.DOTALL | re.IGNORECASE)

                summary = summary_match.group(1).strip() if summary_match else response.text.strip()
                keywords_raw = keywords_match.group(1).strip() if keywords_match else ""
                
                # Clean up keywords, removing brackets and splitting by comma
                keywords = [k.strip() for k in keywords_raw.strip('[]').split(',') if k.strip()]

                return {"summary": summary, "keywords": keywords}

        except Exception:
            logging.error("Failed to generate memory summary.", exc_info=True)
            
        return None

    @tasks.loop(minutes=30)
    async def memory_scheduler(self):
        await self.bot.wait_until_ready()
        logging.info("Memory scheduler starting...")
        for guild in self.bot.guilds:
            messages = []
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).read_message_history:
                    try:
                        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
                        async for message in channel.history(limit=100, after=since):
                            if not message.author.bot:
                                messages.append({
                                    "author": message.author.display_name,
                                    "content": message.content,
                                    "timestamp": message.created_at.isoformat()
                                })
                    except discord.Forbidden:
                        continue
                    except Exception as e:
                        logging.error(f"Could not fetch history for channel '{channel.name}': {e}")
            
            if len(messages) > 5:
                logging.info(f"Generating summary for guild '{guild.name}' with {len(messages)} messages.")
                messages.sort(key=lambda x: x['timestamp'])
                if summary_data := await self._generate_memory_summary(messages):
                    await self.bot.firestore_service.save_memory(str(guild.id), summary_data)
                    logging.info(f"Saved memory summary for guild '{guild.name}'.")
        logging.info("Memory scheduler finished.")

    # --- Tagging Command Helper ---
    async def find_and_tag_member(self, message, user_name: str, times: int = 1):
        MAX_TAGS = 5
        if times > MAX_TAGS:
            await message.channel.send(f"whoa there, buddy. {times} times? you tryna get me banned? i'll do it {MAX_TAGS} times, take it or leave it.")
            times = MAX_TAGS

        if not message.guild:
            await message.channel.send("eh, who am i supposed to tag out here? this is a private chat, pal.")
            return
        
        target_member = discord.utils.find(lambda m: user_name.lower() in m.display_name.lower(), message.guild.members)
        if not target_member:
            target_member = await self._find_user_by_vinny_name(message.guild, user_name)
        
        if target_member:
            try:
                original_command = message.content
                target_nickname = await self.bot.firestore_service.get_user_nickname(str(target_member.id))
                
                name_info = f"Their display name is '{target_member.display_name}'."
                if target_nickname:
                    name_info += f" You know them as '{target_nickname}'."

                tagging_prompt = (
                    f"{self.bot.personality_instruction}\n\n"
                    f"# --- YOUR TASK ---\n"
                    f"A user has given you the command: \"{original_command}\". This requires you to send multiple unique messages.\n\n"
                    f"## CONTEXT:\n"
                    f"- You need to tag: {name_info}.\n"
                    f"- The user wants you to do this **{times} times** in separate messages.\n"
                    f"- **IMPORTANT**: When you speak about this person, you MUST use their nickname ('{target_nickname}') if you know one.\n\n"
                    f"## INSTRUCTIONS:\n"
                    f"Generate a JSON object with a single key, \"messages\", which holds a list of **{times}** strings. Each string must be a short, unique, in-character message that fulfills the user's request. Do not add the user's @mention to the strings; it will be added automatically.\n\n"
                    f"### EXAMPLE OUTPUT FORMAT:\n"
                    f"```json\n"
                    f"{{\n"
                    f'    "messages": ["message one", "message two", "message three"]\n'
                    f"}}\n"
                    f"```"
                )
                
                api_response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=tagging_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if api_response.text:
                    json_string_match = re.search(r'```json\s*(\{.*?\})\s*```', api_response.text, re.DOTALL) or re.search(r'(\{.*?\})', api_response.text, re.DOTALL)
                    message_data = json.loads(json_string_match.group(1))
                    messages_to_send = message_data.get("messages", [])

                    for msg_text in messages_to_send:
                        await message.channel.send(f"{msg_text.strip()} {target_member.mention}")
                        await asyncio.sleep(2)
                    return
            except Exception:
                logging.error("Failed to generate or parse multi-tag response.", exc_info=True)
                await message.channel.send(f"my brain shorted out tryin' to do all that. here, i'll just do it once. hey {target_member.mention}.")
        else:
            await message.channel.send(f"who? i looked all over this joint, couldn't find anyone named '{user_name}'.")
    
    # --- COMMANDS ---
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
        embed.add_field(name="!horoscope [sign]", value="I'll look at the sky and tell ya what's up. It's probably chaos.\n*Example: `!horoscope gemini`*", inline=False)
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
        if await self.bot.firestore_service.save_user_nickname(str(member.id), nickname):
            await ctx.send(f"aight, got it. callin' {member.mention} '{nickname}'.")

    @commands.command(name='forgetme')
    async def forgetme_command(self, ctx):
        if not ctx.guild:
            return await ctx.send("this only works in a server, pal.")
        if await self.bot.firestore_service.delete_user_profile(str(ctx.author.id), str(ctx.guild.id)):
            await ctx.send(f"aight, {ctx.author.mention}. i scrambled my brains. who are you again?")
        else:
            await ctx.send("my head's already empty, pal.")

    @commands.command(name='propose')
    async def propose_command(self, ctx, member: discord.Member):
        if ctx.author == member: return await ctx.send("proposin' to yourself? i get it.")
        if member.bot: return await ctx.send("proposin' to a robot? find a real pulse.")
        if await self.bot.firestore_service.save_proposal(str(ctx.author.id), str(member.id)):
            await ctx.send(f"whoa! {ctx.author.mention} is on one knee for {member.mention}. you got 5 mins to say yes with `!marry @{ctx.author.display_name}`.")

    @commands.command(name='marry')
    async def marry_command(self, ctx, member: discord.Member):
        if not await self.bot.firestore_service.check_proposal(str(member.id), str(ctx.author.id)):
            return await ctx.send(f"{member.display_name} didn't propose to you.")
        if await self.bot.firestore_service.finalize_marriage(str(member.id), str(ctx.author.id)):
            await ctx.send(f":tada: they said yes! i now pronounce {member.mention} and {ctx.author.mention} hitched!")

    @commands.command(name='divorce')
    async def divorce_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        if not profile or "married_to" not in profile:
            return await ctx.send("you ain't married.")
        partner_id = profile["married_to"]
        if await self.bot.firestore_service.process_divorce(str(ctx.author.id), partner_id):
            await ctx.send(f"it's over. {ctx.author.mention} has split from <@{partner_id}>. 📜")

    @commands.command(name='ballandchain')
    async def ballandchain_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        if profile and profile.get("married_to"):
            partner_id = profile.get("married_to")
            partner = self.bot.get_user(int(partner_id)) or await self.bot.fetch_user(int(partner_id))
            partner_name = partner.display_name if partner else "a ghost"
            await ctx.send(f"you're shackled to **{partner_name}**. happened on **{profile.get('marriage_date')}**.")
        else:
            await ctx.send("you ain't married to nobody.")

    @commands.command(name='weather')
    async def weather_command(self, ctx, *, location: str):
        async with ctx.typing():
            coords = await api_clients.geocode_location(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, location)
            if not coords:
                return await ctx.send(f"eh, couldn't find that place '{location}'. you sure that's a real place?")
            weather_data = await api_clients.get_weather_data(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, coords['lat'], coords['lon'])
        
        if not weather_data or weather_data.get("cod") != 200:
            return await ctx.send("found the place but the damn weather report is all garbled.")
        
        try:
            city_name = coords.get("name", weather_data.get("name", "Unknown Location"))
            main_weather = weather_data["weather"][0]
            emoji = constants.get_weather_emoji(main_weather['main'])
            
            embed = discord.Embed(title=f"{emoji} Weather in {city_name}", description=f"**{main_weather.get('description', '').title()}**", color=discord.Color.blue())
            embed.add_field(name="🌡️ Temperature", value=f"{weather_data['main'].get('temp')}°F", inline=True)
            embed.add_field(name="🤔 Feels Like", value=f"{weather_data['main'].get('feels_like')}°F", inline=True)
            embed.add_field(name="💧 Humidity", value=f"{weather_data['main'].get('humidity')}%", inline=True)
            embed.add_field(name="💨 Wind", value=f"{weather_data['wind'].get('speed')} mph", inline=True)
            embed.add_field(name="📡 Live Radar", value=f"[Click to View](https://www.windy.com/{coords['lat']}/{coords['lon']})", inline=False)
            embed.set_footer(text="don't blame me if the sky starts lyin'. salute!")
            embed.timestamp = datetime.datetime.now(datetime.UTC)
            await ctx.send(embed=embed)
        except Exception:
            logging.error("Failed to create weather embed.", exc_info=True)
            await ctx.send("somethin' went wrong with the damn weather machine.")

    @commands.command(name='horoscope')
    async def horoscope_command(self, ctx, *, sign: str):
        valid_signs = list(constants.SIGN_EMOJIS.keys())
        clean_sign = sign.lower()

        if clean_sign not in valid_signs:
            return await ctx.send(f"'{sign}'? that ain't a star sign, pal. try one of these: {', '.join(valid_signs)}.")
        
        async with ctx.typing():
            horoscope_data = await api_clients.get_horoscope(self.bot.http_session, clean_sign)
            if not horoscope_data:
                return await ctx.send("the stars are all fuzzy today. couldn't get a readin'. maybe they're drunk.")

            boring_horoscope = horoscope_data.get('horoscope_data', "The stars ain't talkin' today.")
            vinnyfied_text = boring_horoscope
            try:
                rewrite_prompt = (
                    f"{self.bot.personality_instruction}\n\n"
                    f"# --- YOUR TASK ---\n"
                    f"You must rewrite a boring horoscope into a chaotic, flirty, and slightly unhinged one in your own voice. "
                    f"The user's sign is **{clean_sign.title()}**. The boring horoscope is: \"{boring_horoscope}\"\n\n"
                    f"## INSTRUCTIONS:\n"
                    f"Generate a short, single-paragraph monologue that gives the user their horoscope in your unique, chaotic style. Do not just repeat the horoscope; interpret it with your personality."
                )
                response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[types.Content(role='user', parts=[types.Part(text=rewrite_prompt)])],
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                if response.text:
                    vinnyfied_text = response.text.strip()
            except Exception:
                logging.error("Failed to Vinny-fy the horoscope.", exc_info=True)

            emoji = constants.SIGN_EMOJIS.get(clean_sign, "✨")
            embed = discord.Embed(
                title=f"{emoji} Horoscope for {clean_sign.title()}",
                description=vinnyfied_text,
                color=discord.Color.dark_purple()
            )
            embed.set_thumbnail(url="https://i.imgur.com/4laks52.gif")
            embed.set_footer(text="don't blame me if the stars lie. they're drama queens.")
            embed.timestamp = datetime.datetime.now(ZoneInfo("America/New_York"))
            await ctx.send(embed=embed)

    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        target_user = ctx.author
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]
            knowledge_string = re.sub(f'<@!?{target_user.id}>', target_user.display_name, knowledge_string).strip()
        
        extracted_facts = await extract_facts_from_message(self.bot, knowledge_string)
        
        if not extracted_facts:
            logging.warning(f"Fact extraction failed for string: '{knowledge_string}'")
            await ctx.send("eh? what're you tryin' to tell me? i didn't get that. try sayin' it like 'my favorite food is pizza'.")
            return

        saved_facts = []
        guild_id = str(ctx.guild.id) if ctx.guild else None
        for key, value in extracted_facts.items():
            if await self.bot.firestore_service.save_user_profile_fact(str(target_user.id), guild_id, key, value):
                saved_facts.append(f"'{key}' is '{value}'")

        if saved_facts:
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
            except Exception:
                logging.error("Failed to generate dynamic confirmation for !vinnyknows.", exc_info=True)
                await ctx.send(f"aight, i got it. so {'your' if target_user == ctx.author else f'{target_user.display_name}\'s'} {facts_confirmation}. vinny will remember.")
        else:
            await ctx.send("my head's all fuzzy. tried to remember that but it slipped out.")

    # --- Owner-Only Commands ---
    @commands.command(name='autonomy')
    @commands.is_owner()
    async def autonomy_command(self, ctx, status: str):
        if status.lower() == 'on':
            self.bot.autonomous_mode_enabled = True
            await ctx.send("aight, vinny's off the leash.")
        elif status.lower() == 'off':
            self.bot.autonomous_mode_enabled = False
            await ctx.send("thank god. vinny's back in his cage.")
        else:
            await ctx.send("it's 'on' or 'off', pal. pick one.")

    @commands.command(name='set_relationship')
    @commands.is_owner()
    async def set_relationship_command(self, ctx, member: discord.Member, rel_type: str):
        valid_types = ['friends', 'rivals', 'distrusted', 'admired', 'annoyance', 'neutral']
        if rel_type.lower() in valid_types:
            guild_id = str(ctx.guild.id) if ctx.guild else None
            if await self.bot.firestore_service.save_user_profile_fact(str(member.id), guild_id, 'relationship_status', rel_type.lower()):
                await ctx.send(f"aight, got it. me and {member.display_name} are... '{rel_type.lower()}'.")
        else:
            await ctx.send(f"that ain't a real relationship type. try: {', '.join(valid_types)}")

    @commands.command(name='clear_memories')
    @commands.is_owner()
    async def clear_memories_command(self, ctx):
        if not ctx.guild:
            return await ctx.send("can't clear memories from a private chat, pal.")
        
        path = constants.get_summaries_collection_path(self.bot.APP_ID, str(ctx.guild.id))
        if await self.bot.firestore_service.delete_docs(path):
            await ctx.send("aight, it's done. all the old chatter is gone.")
        else:
            await ctx.send("couldn't clear the memories. maybe they're stuck.")


async def setup(bot):
    await bot.add_cog(VinnyLogic(bot))