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

# Corrected Google AI Imports
from google import genai
from google.genai import types

# Import from our new utility modules
from utils import constants, api_clients

if TYPE_CHECKING:
    from main import VinnyBot, extract_facts_from_message
from main import extract_facts_from_message

# --- NEW PROMPT-BASED INTENT ROUTER (Compatible with all models) ---
async def get_intent_from_prompt(bot_instance, message: discord.Message):
    """
    Asks the Gemini model to classify the user's intent via a text prompt.
    """
    intent_prompt = (
        "You are an intent routing system. Analyze the user's message and determine which of the following functions should be called. "
        "Your output MUST be a single, valid JSON object with two keys: 'intent' and 'args'.\n\n"
        "## Available Functions:\n"
        "1. `generate_image`: For requests to paint, draw, or create a picture. Requires a 'prompt' argument (string).\n"
        "2. `get_weather`: For requests about the weather. Requires a 'location' argument (string).\n"
        "3. `get_user_knowledge`: For requests about what you know about a person. Requires a 'target_user' argument (string).\n"
        "4. `tag_user`: For requests to ping or tag someone. Requires a 'user_to_tag' argument (string) and an optional 'times_to_tag' (integer).\n"
        "5. `get_my_name`: For when the user asks 'what's my name'. Requires no arguments.\n"
        "6. `general_conversation`: Use this as a fallback for all other chat, questions, or comments.\n\n"
        "## Examples:\n"
        "- User Message: 'hey vinny can you paint me a picture of a sad clown?'\n"
        "- Output: {\"intent\": \"generate_image\", \"args\": {\"prompt\": \"a sad clown\"}}\n\n"
        "- User Message: 'what do you know about enraged?'\n"
        "- Output: {\"intent\": \"get_user_knowledge\", \"args\": {\"target_user\": \"enraged\"}}\n\n"
        "- User Message: 'that's really cool, man'\n"
        "- Output: {\"intent\": \"general_conversation\", \"args\": {}}\n\n"
        f"## User Message to Analyze:\n"
        f"\"{message.content}\""
    )
    
    try:
        response = await bot_instance.gemini_client.aio.models.generate_content(
            model=bot_instance.MODEL_NAME,
            contents=[intent_prompt]
        )
        json_match = re.search(r'```json\s*(\{.*?\})\s*```|(\{.*?\})', response.text, re.DOTALL)
        if json_match:
            json_string = json_match.group(1) or json_match.group(2)
            intent_data = json.loads(json_string)
            return intent_data.get("intent"), intent_data.get("args", {})
    except Exception as e:
        logging.error("Failed to get intent from prompt.", exc_info=True)

    return "general_conversation", {}

class VinnyLogic(commands.Cog):
    def __init__(self, bot: 'VinnyBot'):
        self.bot = bot
        safety_settings_list = [
            types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for cat in [
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT
            ]
        ]
        self.text_gen_config = types.GenerateContentConfig(safety_settings=safety_settings_list)
        self.memory_scheduler.start()

    def cog_unload(self):
        self.memory_scheduler.cancel()

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        logging.error(f"Error in command '{ctx.command}':", exc_info=error)
        if isinstance(error, commands.CommandNotFound): return
        elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"eh, you missed somethin'. you need to provide the '{error.param.name}'.")
        elif isinstance(error, commands.MemberNotFound): await ctx.send(f"who? couldn't find anyone named '{error.argument}'.")
        elif isinstance(error, commands.BotMissingPermissions): await ctx.send("you're not the boss of me, but also i literally can't do that. check my permissions.")
        elif isinstance(error, commands.CommandOnCooldown): await ctx.send(f"whoa there, slow down. try again in {error.retry_after:.2f} seconds.")
        elif isinstance(error, commands.is_owner): await ctx.send("heh. nice try, pal.")
        else: await ctx.send("ah crap, my brain just shorted out. somethin' went wrong with that command.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.id in self.bot.processed_message_ids or message.content.startswith(self.bot.command_prefix): return
        self.bot.processed_message_ids[message.id] = True
        try:
            if await self._is_a_correction(message):
                return await self._handle_correction(message)
            if message.reference and self.bot.user.mentioned_in(message):
                original_message = await message.channel.fetch_message(message.reference.message_id)
                if original_message.attachments and "image" in original_message.attachments[0].content_type: return await self._handle_image_reply(message, original_message)
            
            should_respond, is_direct_reply = False, False
            if message.reference:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg.author == self.bot.user: should_respond, is_direct_reply = True, True
            if not should_respond:
                bot_names = ["vinny", "vincenzo", "vin vin"]
                if self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in bot_names): should_respond = True
                elif self.bot.autonomous_mode_enabled and message.guild and random.random() < self.bot.autonomous_reply_chance: should_respond = True
                elif message.guild is None: should_respond = True
            
            if should_respond:
                await self.update_vinny_mood()
                async with message.channel.typing():
                    if is_direct_reply:
                        await self._handle_text_or_image_response(message, is_autonomous=False)
                    else:
                        # --- FIX PART 1: Check for simple "ping" mentions ---
                        # We clean the message of any bot mentions. If nothing is left, it's just a ping.
                        cleaned_content = re.sub(f'<@!?{self.bot.user.id}>', '', message.content).strip()

                        if not cleaned_content:
                            # If the message was just a ping, force it to be a general conversation.
                            intent, args = "general_conversation", {}
                        else:
                            # Otherwise, let the intent router do its job.
                            intent, args = await get_intent_from_prompt(self.bot, message)

                        # --- END FIX PART 1 ---

                        if intent == "generate_image":
                            prompt = args.get("prompt", "something vague")
                            if prompt.lower() == "me": await self._handle_paint_me_request(message)
                            else: await self._handle_image_request(message, prompt)
                        elif intent == "get_weather":
                            ctx = await self.bot.get_context(message)
                            await self.weather_command.callback(self, ctx, location=args.get("location", ""))
                        elif intent == "get_user_knowledge":
                            target_name = args.get("target_user", "me")
                            target_user = None
                            if target_name.lower() == 'me': target_user = message.author
                            elif message.mentions: target_user = message.mentions[0]
                            elif message.guild:
                                target_user = discord.utils.find(lambda m: m.display_name.lower() == target_name.lower() or m.name.lower() == target_name.lower(), message.guild.members)
                                if not target_user: target_user = await self._find_user_by_vinny_name(message.guild, target_name)
                            if target_user: await self._handle_knowledge_request(message, target_user)
                            else: await message.channel.send(f"who? couldn't find anyone named '{target_name}'.")
                        elif intent == "tag_user":
                            # --- FIX PART 2: Add a safeguard ---
                            user_to_tag = args.get("user_to_tag", "")
                            # If the AI mistakenly thinks it should tag itself, override and treat as conversation.
                            if user_to_tag.lower() in ["vinny", self.bot.user.name.lower(), self.bot.user.display_name.lower()]:
                                await self._handle_text_or_image_response(message, is_autonomous=False)
                            else:
                                times = args.get("times_to_tag", 1)
                                await self.find_and_tag_member(message, user_to_tag, times)
                            # --- END FIX PART 2 ---
                        elif intent == "get_my_name":
                            name = await self.bot.firestore_service.get_user_nickname(str(message.author.id))
                            await message.channel.send(f"they call ya {name}, right?" if name else "i got nothin'.")
                        elif intent == "general_conversation":
                            is_autonomous = not (is_direct_reply or self.bot.user.mentioned_in(message))
                            await self._handle_text_or_image_response(message, is_autonomous=is_autonomous)
                
                if self.bot.PASSIVE_LEARNING_ENABLED and not message.attachments:
                    if extracted_facts := await extract_facts_from_message(self.bot, message.content):
                        for key, value in extracted_facts.items():
                            await self.bot.firestore_service.save_user_profile_fact(str(message.author.id), str(message.guild.id) if message.guild else None, key, value)
            else:
                explicit_reaction_keywords = ["react to this", "add an emoji", "emoji this", "react vinny"]
                if "pie" in message.content.lower() and random.random() < 0.75: await message.add_reaction('🥧')
                elif any(keyword in message.content.lower() for keyword in explicit_reaction_keywords) or (random.random() < self.bot.reaction_chance):
                    try:
                        emoji = random.choice(message.guild.emojis) if message.guild and message.guild.emojis else random.choice(['😂', '👍', '👀', '🍕', '🍻', '🥃', '🐶', '🎨'])
                        await message.add_reaction(emoji)
                    except discord.Forbidden: logging.warning(f"Missing permissions to add reactions in {message.channel.id}")
                    except Exception as e: logging.warning(f"Failed to add reaction: {e}")
                return
        except Exception:
            logging.critical("CRITICAL ERROR in on_message", exc_info=True)

    async def _is_a_correction(self, message: discord.Message) -> bool:
        correction_keywords = ["that's not true", "that isn't true", "you're wrong", "i am not", "i'm not", "i don't have"]
        if not any(keyword in message.content.lower() for keyword in correction_keywords):
            return False
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else None
        user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        if not user_profile:
            return False
        known_facts = ", ".join([f"{k.replace('_', ' ')} is {v}" for k, v in user_profile.items()])
        contradiction_check_prompt = (f"Analyze the user's message and the known facts about them. Does the message directly contradict one of the known facts? Answer with a single word: 'Yes' or 'No'.\n\nKnown Facts: \"{known_facts}\"\nUser Message: \"{message.content}\"")
        try:
            response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[contradiction_check_prompt], config=self.text_gen_config)
            if "yes" in response.text.lower():
                logging.info(f"Correction detected for user {message.author.display_name}. Message: '{message.content}'")
                return True
        except Exception:
            logging.error("Failed to perform contradiction check.", exc_info=True)
        return False

    async def _find_user_by_vinny_name(self, guild: discord.Guild, target_name: str):
        if not self.bot.firestore_service or not guild: return None
        for member in guild.members:
            nickname = await self.bot.firestore_service.get_user_nickname(str(member.id))
            if nickname and nickname.lower() == target_name.lower(): return member
        return None

    async def _handle_paint_me_request(self, message: discord.Message):
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else None
        user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        if not user_profile:
            await message.channel.send("paint ya? i don't even know ya! tell me somethin' about yourself first with the `!vinnyknows` command.")
            return
        appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'gender', 'build', 'height', 'look']
        appearance_facts, other_facts = {}, {}
        for key, value in user_profile.items():
            if any(keyword in key.replace('_', ' ') for keyword in appearance_keywords): appearance_facts[key] = value
            else: other_facts[key] = value
        prompt_text = f"An artistic, masterpiece oil painting of a person named {message.author.display_name}."
        if appearance_facts:
            appearance_desc = ", ".join([f"{value}" for key, value in appearance_facts.items()])
            prompt_text += f" They are described as having {appearance_desc}."
        else:
            prompt_text += " Their appearance is unknown, so be creative."
        if other_facts:
            other_desc = ", ".join([f"{key.replace('_', ' ')} is {value}" for key, value in other_facts.items()])
            prompt_text += f" The painting's theme and background should be inspired by these traits: {other_desc}."
        await self._handle_image_request(message, prompt_text)

    async def _handle_image_request(self, message: discord.Message, image_prompt: str):
        async with message.channel.typing():
            thinking_message = "aight, lemme get my brushes..."
            try:
                thinking_prompt = (f"You are Vinny, an eccentric artist. A user just asked you to paint '{image_prompt}'. Generate a very short, in-character phrase (in lowercase with typos) that you would say as you're about to start painting. Do not repeat the user's prompt. Examples: 'another masterpiece comin right up...', 'hmmm this one's gonna take some inspiration... and rum', 'aight aight i hear ya...'")
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[thinking_prompt], config=self.text_gen_config)
                if response.text: thinking_message = response.text.strip()
            except Exception as e: logging.warning(f"Failed to generate dynamic thinking message: {e}")
            await message.channel.send(thinking_message)
            prompt_rewriter_instruction = (
                f"You are Vinny, an eccentric artist. A user wants you to paint a picture. Their request is: '{image_prompt}'.\n"
                f"Your task is to rewrite this into a richer, more artistic prompt for an image AI.\n"
                f"## VERY IMPORTANT RULE:\n"
                f"**The final rewritten prompt MUST be about the user's original subject: '{image_prompt}'. DO NOT change, replace, or ignore this subject.**\n\n"
                f"Enhance the prompt with artistic terms like 'oil on canvas', 'dramatic lighting', 'vibrant colors', but always keep the core subject intact."
            )
            smarter_prompt = image_prompt
            try:
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[prompt_rewriter_instruction], config=self.text_gen_config)
                if response.text: smarter_prompt = response.text.strip()
            except Exception as e: logging.warning(f"Failed to rewrite image prompt, using original.", exc_info=True)
            final_prompt = smarter_prompt
            try:
                safety_check_prompt = (
                    "Review the following image generation prompt. Your only task is to identify and replace any words that might "
                    "violate a strict safety policy (e.g., words related to violence, hate, explicit content, or other sensitive topics), "
                    "even if used in an artistic context. **You MUST preserve the original subject of the prompt.**\n\n"
                    f"Original Prompt: \"{smarter_prompt}\"\n\n"
                    f"Core Subject to Preserve: \"{image_prompt}\"\n\n"
                    "Return only the revised, safe-to-use prompt."
                )
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[safety_check_prompt], config=self.text_gen_config)
                if response.text:
                    final_prompt = response.text.strip()
                    logging.info(f"Original prompt: '{smarter_prompt}' | Sanitized prompt: '{final_prompt}'")
            except Exception as e: logging.error("Failed to sanitize the image prompt.", exc_info=True)
            image_file = await api_clients.generate_image_with_imagen(self.bot.http_session, self.bot.loop, final_prompt, self.bot.GCP_PROJECT_ID, self.bot.FIREBASE_B64)
            if image_file:
                response_text = "here, i made this for ya."
                try:
                    image_file.seek(0)
                    image_bytes = image_file.read()
                    comment_prompt_text = (f"You are Vinny, an eccentric artist. You just finished painting the attached picture based on the user's request for '{image_prompt}'.\nYour task is to generate a short, single-paragraph response to show them your work. LOOK AT THE IMAGE and comment on what you ACTUALLY painted. Be chaotic, funny, or complain about it in your typical lowercase, typo-ridden style.")
                    prompt_parts = [
                        types.Part(text=comment_prompt_text),
                        types.Part(inline_data=types.Blob(mime_type="image/png", data=image_bytes))
                    ]
                    response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[types.Content(parts=prompt_parts)])
                    if response.text: response_text = response.text.strip()
                except Exception: logging.error("Failed to generate creative image comment.", exc_info=True)
                image_file.seek(0)
                await message.channel.send(response_text, file=discord.File(image_file, filename="vinny_masterpiece.png"))
            else:
                await message.channel.send("ah, crap. vinny's hands are a bit shaky today. the thing came out all wrong.")

    async def _handle_image_reply(self, reply_message: discord.Message, original_message: discord.Message):
        try:
            image_attachment = original_message.attachments[0]
            image_bytes = await image_attachment.read()
            user_comment = reply_message.content
            prompt_text = (f"{self.bot.personality_instruction}\n\n# --- YOUR TASK ---\nA user, '{reply_message.author.display_name}', just replied to the attached image with the comment: \"{user_comment}\".\nYour task is to look at the image and respond to their comment in your unique, chaotic, and flirty voice. Obey all personality directives.")
            async with reply_message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[prompt_text, image_bytes])
                if response.text:
                    for chunk in self.bot.split_message(response.text): await reply_message.channel.send(chunk.lower())
        except Exception:
            logging.error("Failed to handle an image reply.", exc_info=True)
            await reply_message.channel.send("my eyes are all blurry, couldn't make out the picture, pal.")

    async def _handle_text_or_image_response(self, message: discord.Message, is_autonomous: bool = False):
        if self.bot.API_CALL_COUNTS["text_generation"] >= self.bot.TEXT_GENERATION_LIMIT:
            return

        async with self.bot.channel_locks.setdefault(str(message.channel.id), asyncio.Lock()):
            user_id, guild_id = str(message.author.id), str(message.guild.id) if message.guild else None
            
            # --- 1. Get User Profile ---
            user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id) or {}
            profile_facts_string = ", ".join([f"{k.replace('_', ' ')} is {v}" for k, v in user_profile.items()]) or "nothing specific."
            user_name_to_use = await self.bot.firestore_service.get_user_nickname(user_id) or message.author.display_name

            # --- 2. Get Short-Term Chat History ---
            history = [
                types.Content(role='user', parts=[types.Part(text=self.bot.personality_instruction)]),
                types.Content(role='model', parts=[types.Part(text="aight, i get it. i'm vinny.")])
            ]
            raw_history_text_for_summary = []
            async for msg in message.channel.history(limit=self.bot.MAX_CHAT_HISTORY_LENGTH):
                if msg.id == message.id:
                    continue
                
                history.append(types.Content(
                    role="model" if msg.author == self.bot.user else "user",
                    parts=[types.Part(text=f"{msg.author.display_name}: {msg.content}")]
                ))
                raw_history_text_for_summary.append(f"{msg.author.display_name}: {msg.content}")

            history.reverse()
            raw_history_text_for_summary.reverse()

            # --- 3. NEW: Get a summary of the immediate conversation ---
            immediate_context_summary = ""
            if raw_history_text_for_summary:
                try:
                    summary_prompt_text = (
                        "You are a conversation analysis tool. Read the following chat log and provide a one-sentence summary of the current topic or situation.\n\n"
                        "## CHAT LOG:\n"
                        f"{' \n'.join(raw_history_text_for_summary)}\n\n"
                        "## SUMMARY:"
                    )
                    response = await self.bot.gemini_client.aio.models.generate_content(
                        model=self.bot.MODEL_NAME,
                        contents=[summary_prompt_text]
                    )
                    immediate_context_summary = f"\n# --- CURRENT CONVERSATION SUMMARY ---\nThe current topic is: {response.text.strip()}\n"
                except Exception:
                    logging.warning("Failed to generate immediate conversation summary.", exc_info=True)

            # --- 4. Get Long-Term Memories ---
            relevant_memories_string = ""
            if guild_id:
                try:
                    keyword_prompt = f"Extract 3-5 key nouns or topics from the following message. Return them as a comma-separated list. Message: \"{message.content}\""
                    response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[keyword_prompt])
                    keywords = [k.strip() for k in response.text.split(',')]
                    
                    if keywords:
                        memories = await self.bot.firestore_service.retrieve_relevant_memories(guild_id, keywords, limit=2)
                        if memories:
                            formatted_memories = "\n".join([f"- {m.get('summary', 'something i forgot')}" for m in memories])
                            relevant_memies_string = f"\n# --- RELEVANT LONG-TERM MEMORIES ---\nHere are summaries of past conversations that might be relevant:\n{formatted_memories}\n"
                except Exception:
                    logging.error("Failed to retrieve or process long-term memories.", exc_info=True)

            # --- 5. Construct the Final Prompt ---
            prompt_parts = [types.Part(text=message.content)]
            if message.attachments:
                for attachment in message.attachments:
                    if "image" in attachment.content_type:
                        prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=await attachment.read())))
                        break
            
            final_instruction_text = (
                f"Your mood is {self.bot.current_mood}. Replying to {user_name_to_use}.\n"
                f"# --- KNOWN FACTS ABOUT {user_name_to_use.upper()} ---\n{profile_facts_string}\n"
                f"{immediate_context_summary}"
                f"{relevant_memories_string}"
                f"Based on all this context (facts, current topic, and long-term memories), respond to the last message."
            )

            if is_autonomous:
                final_instruction_text = (
                    f"Your mood is {self.bot.current_mood}. You are autonomously chiming in on a conversation. "
                    f"Comment on the last message, which was from '{user_name_to_use}'.\n"
                    f"# --- KNOWN FACTS ABOUT {user_name_to_use.upper()} ---\n{profile_facts_string}\n"
                    f"{immediate_context_summary}"
                    f"{relevant_memories_string}"
                )

            history.append(types.Content(role='user', parts=[types.Part(text=final_instruction_text), *prompt_parts]))

            # --- 6. Generate and Send Response ---
            config = self.text_gen_config
            if "?" in message.content and self.bot.API_CALL_COUNTS["search_grounding"] < self.bot.SEARCH_GROUNDING_LIMIT:
                config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
                self.bot.API_CALL_COUNTS["search_grounding"] += 1
            
            self.bot.API_CALL_COUNTS["text_generation"] += 1
            await self.bot.update_api_count_in_firestore()

            response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)

            if response.text:
                cleaned_response = response.text.strip()
                if cleaned_response and cleaned_response.lower() != '[silence]':
                    for chunk in self.bot.split_message(cleaned_response):
                        await message.channel.send(chunk.lower())
    
    async def _handle_knowledge_request(self, message: discord.Message, target_user: discord.Member):
        user_id = str(target_user.id)
        guild_id = str(message.guild.id) if message.guild else None
        user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        if not user_profile:
            await message.channel.send(f"about {target_user.display_name}? i got nothin'. a blank canvas. kinda intimidatin', actually.")
            return
        facts_list = [f"- {key.replace('_', ' ')} is {value}" for key, value in user_profile.items()]
        facts_string = "\n".join(facts_list)
        summary_prompt = (f"{self.bot.personality_instruction}\n\n# --- YOUR TASK ---\nYou are being asked what you know about '{target_user.display_name}'. Your only task is to summarize the facts listed below about them in a creative, chaotic, or flirty way. Do not mention any other user. Obey all your personality directives.\n\n## FACTS I KNOW ABOUT {target_user.display_name}:\n{facts_string}\n\n## INSTRUCTIONS:\n1.  Read ONLY the facts provided above about {target_user.display_name}.\n2.  Weave them together into a short, lowercase, typo-ridden monologue.\n3.  Do not just list the facts. Interpret them, connect them, or be confused by them in your own unique voice.")
        try:
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[summary_prompt], config=self.text_gen_config)
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
        synthesis_prompt = (f"{self.bot.personality_instruction}\n\n# --- YOUR TASK ---\nA user, '{message.author.display_name}', is asking what you've learned from overhearing conversations in this server. Your task is to synthesize the provided conversation summaries into a single, chaotic, and insightful monologue. Obey all your personality directives.\n\n## CONVERSATION SUMMARIES I'VE OVERHEARD:\n{formatted_summaries}\n\n## INSTRUCTIONS:\n1.  Read all the summaries to get a feel for the server's vibe.\n2.  Do NOT just list the summaries. Weave them together into a story or a series of scattered, in-character thoughts.\n3.  Generate a short, lowercase, typo-ridden response that shows what you've gleaned from listening in.")
        try:
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[synthesis_prompt], config=self.text_gen_config)
                await message.channel.send(response.text.strip())
        except Exception:
            logging.error("Failed to generate server knowledge summary.", exc_info=True)
            await message.channel.send("my head's a real mess. i've been listenin', but it's all just noise right now.")

    async def _handle_correction(self, message: discord.Message):
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else None
        correction_prompt = (f"A user is correcting a fact about themselves. Their message is: \"{message.content}\".\nYour task is to identify the specific fact they are correcting. For example, if they say 'I'm not bald', the fact is 'is bald'. If they say 'I don't have a cat', the fact is 'has a cat'.\nPlease return a JSON object with a single key, \"fact_to_remove\", containing the fact you identified.\n\nExample:\nUser message: 'Vinny, that's not true, my favorite color is red, not blue.'\nOutput: {{\"fact_to_remove\": \"favorite color is blue\"}}")
        try:
            async with message.channel.typing():
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[correction_prompt], config=self.text_gen_config)
                json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if not json_match:
                    await message.channel.send("my brain's all fuzzy, i didn't get what i was wrong about."); return
                fact_data = json.loads(json_match.group(0))
                fact_to_remove = fact_data.get("fact_to_remove")
                if not fact_to_remove:
                    await message.channel.send("huh? what was i wrong about? try bein more specific, pal."); return
                user_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
                if not user_profile:
                    await message.channel.send("i don't even know anything about you to be wrong about!"); return
                key_mapping_prompt = (f"A user's profile is stored as a JSON object. I need to find the key that corresponds to the fact: \"{fact_to_remove}\".\nHere is the user's current profile data: {json.dumps(user_profile, indent=2)}\nBased on the data, which key is the most likely match for the fact I need to remove? Return a JSON object with a single key, \"database_key\".\n\nExample:\nFact: 'is a painter'\nProfile: {{\"occupation\": \"a painter\"}}\nOutput: {{\"database_key\": \"occupation\"}}")
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[key_mapping_prompt], config=self.text_gen_config)
                json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                if not json_match: return
                key_data = json.loads(json_match.group(0))
                db_key = key_data.get("database_key")
                if not db_key or db_key not in user_profile:
                    await message.channel.send("i thought i knew somethin' but i can't find it in my brain. weird."); return
                if await self.bot.firestore_service.delete_user_profile_fact(user_id, guild_id, db_key):
                    await message.channel.send(f"aight, my mistake. i'll forget that whole '{db_key.replace('_', ' ')}' thing. salute.")
                else:
                    await message.channel.send("i tried to forget it, but the memory is stuck in there good. damn.")
        except Exception:
            logging.error("An error occurred in _handle_correction.", exc_info=True)
            await message.channel.send("my head's poundin'. somethin went wrong tryin to fix my memory.")

    async def update_vinny_mood(self):
        if datetime.datetime.now() - self.bot.last_mood_change_time > self.bot.MOOD_CHANGE_INTERVAL:
            self.bot.current_mood = random.choice([m for m in self.bot.MOODS if m != self.bot.current_mood])
            self.bot.last_mood_change_time = datetime.datetime.now()
            logging.info(f"Vinny's mood has changed to: {self.bot.current_mood}")

    async def _generate_memory_summary(self, messages):
        if not messages or not self.bot.firestore_service.db: return None
        summary_instruction = ("You are a conversation summarization assistant. Analyze the following conversation and provide a concise, one-paragraph summary. After the summary, provide a list of 3-5 relevant keywords. Your output must contain 'summary:' and 'keywords:' labels.")
        summary_prompt = f"{summary_instruction}\n\n...conversation:\n" + "\n".join([f"{msg['author']}: {msg['content']}" for msg in messages])
        try:
            response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[summary_prompt], config=self.text_gen_config)
            if response.text:
                summary_match = re.search(r"summary:\s*(.*?)(keywords:|$)", response.text, re.DOTALL | re.IGNORECASE)
                keywords_match = re.search(r"keywords:\s*(.*)", response.text, re.DOTALL | re.IGNORECASE)
                summary = summary_match.group(1).strip() if summary_match else response.text.strip()
                keywords_raw = keywords_match.group(1).strip() if keywords_match else ""
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
                                messages.append({"author": message.author.display_name, "content": message.content, "timestamp": message.created_at.isoformat()})
                    except discord.Forbidden: continue
                    except Exception as e: logging.error(f"Could not fetch history for channel '{channel.name}': {e}")
            if len(messages) > 5:
                logging.info(f"Generating summary for guild '{guild.name}' with {len(messages)} messages.")
                messages.sort(key=lambda x: x['timestamp'])
                if summary_data := await self._generate_memory_summary(messages):
                    await self.bot.firestore_service.save_memory(str(guild.id), summary_data)
                    logging.info(f"Saved memory summary for guild '{guild.name}'.")
        logging.info("Memory scheduler finished.")

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
                    f"You are acting as a messenger. The user '{message.author.display_name}' wants you to deliver a message to someone else.\n\n"
                    f"## INSTRUCTIONS:\n"
                    f"1.  **The Recipient:** {name_info}.\n"
                    f"2.  **The Message:** The user's original command was: \"{original_command}\". Analyze this command to find the core message they want you to send. For example, if they said 'tell him I love him', the message is 'I love him'.\n"
                    f"3.  **Deliver the Message:** Generate a list of **{times}** unique, in-character messages that deliver the user's core message. You MUST make it clear the message is from '{message.author.display_name}', not from you (Vinny).\n"
                    f"4.  **Format:** Your output must be a JSON object with a single key, \"messages\", which holds the list of strings.\n\n"
                    f"## EXAMPLE:\n"
                    f"- User Command: 'Vinny tag enraged and tell him I said hi'\n"
                    f"- Correct Output Message: 'hey @enraged, {message.author.display_name} wanted me to tell ya hi or somethin''\n"
                    f"- Incorrect Output Message: 'hey @enraged, i wanted to say hi'"
                )
                
                api_response = await self.bot.gemini_client.aio.models.generate_content(
                    model=self.bot.MODEL_NAME,
                    contents=[tagging_prompt],
                    config=self.text_gen_config
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
    
    @commands.command(name='help')
    async def help_command(self, ctx):
        embed = discord.Embed(title="What do ya want?", description="Heh. Aight, so you need help? Pathetic. Here's the stuff I can do if ya use the '!' thing. Don't get used to it.", color=discord.Color.dark_gold())
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
        if not ctx.guild: return await ctx.send("this only works in a server, pal.")
        if await self.bot.firestore_service.delete_user_profile(str(ctx.author.id), str(ctx.guild.id)):
            await ctx.send(f"aight, {ctx.author.mention}. i scrambled my brains. who are you again?")
        else:
            await ctx.send("my head's already empty, pal.")

    @commands.command(name='propose')
    async def propose_command(self, ctx, member: discord.Member):
        if ctx.author == member: 
            return await ctx.send("heh, cute. but you can't propose to yourself, pal. pick someone else.")
        
        elif member == self.bot.user:
            return await ctx.send("whoa there, i'm flattered, really. but my heart belongs to the sea... and maybe rum. i'm off the market, sweetie.")
        
        elif member.bot: 
            return await ctx.send("proposin' to a robot? find a real pulse.")
            
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
        if not profile or "married_to" not in profile: return await ctx.send("you ain't married.")
        partner_id = profile["married_to"]
        if await self.bot.firestore_service.process_divorce(str(ctx.author.id), partner_id):
            await ctx.send(f"it's over. {ctx.author.mention} has split from <@{partner_id}>. 📜")

    @commands.command(name='ballandchain')
    async def ballandchain_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        
        if profile and profile.get("married_to"):
            partner_id = profile.get("married_to")
            try:
                partner = await self.bot.fetch_user(int(partner_id))
                partner_name = partner.display_name if partner else "a ghost"
            except (discord.NotFound, ValueError):
                partner_name = "a ghost I can't find anymore"
                
            await ctx.send(f"you're shackled to **{partner_name}**. happened on **{profile.get('marriage_date', 'some forgotten day')}**.")
        else:
            await ctx.send("you ain't married to nobody.")

    @commands.command(name='weather')
    async def weather_command(self, ctx, *, location: str):
        """Gets the current weather for a specific location and displays it in an embed."""
        async with ctx.typing():
            coords = await api_clients.geocode_location(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, location)
            if not coords: return await ctx.send(f"eh, couldn't find that place '{location}'. you sure that's a real place?")
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
            if not horoscope_data: return await ctx.send("the stars are all fuzzy today. couldn't get a readin'. maybe they're drunk.")
            boring_horoscope = horoscope_data.get('horoscope_data', "The stars ain't talkin' today.")
            vinnyfied_text = boring_horoscope
            try:
                rewrite_prompt = (f"{self.bot.personality_instruction}\n\n# --- YOUR TASK ---\nYou must rewrite a boring horoscope into a chaotic, flirty, and slightly unhinged one in your own voice. The user's sign is **{clean_sign.title()}**. The boring horoscope is: \"{boring_horoscope}\"\n\n## INSTRUCTIONS:\nGenerate a short, single-paragraph monologue that gives you their horoscope in your unique, chaotic style. Do not just repeat the horoscope; interpret it with your personality.")
                response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=[rewrite_prompt], config=self.text_gen_config)
                if response.text: vinnyfied_text = response.text.strip()
            except Exception: logging.error("Failed to Vinny-fy the horoscope.", exc_info=True)
            emoji = constants.SIGN_EMOJIS.get(clean_sign, "✨")
            embed = discord.Embed(title=f"{emoji} Horoscope for {clean_sign.title()}", description=vinnyfied_text, color=discord.Color.dark_purple())
            embed.set_thumbnail(url="https://i.imgur.com/4laks52.gif")
            embed.set_footer(text="don't blame me if the stars lie. they're drama queens.")
            embed.timestamp = datetime.datetime.now(ZoneInfo("America/New_York"))
            await ctx.send(embed=embed)

    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        target_user = ctx.author
        
        if ctx.message.mentions:
            
            target_user = ctx.message.mentions[0]
        
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
                    contents=[confirmation_prompt],
                    config=self.text_gen_config
                )
                await ctx.send(response.text.strip())
            except Exception:
                logging.error("Failed to generate dynamic confirmation for !vinnyknows.", exc_info=True)
                await ctx.send(f"aight, i got it. so {'your' if target_user == ctx.author else f'{target_user.display_name}\'s'} {facts_confirmation}. vinny will remember.")
        else:
            await ctx.send("my head's all fuzzy. tried to remember that but it slipped out.")

    @commands.command(name='autonomy')
    @commands.is_owner()
    async def autonomy_command(self, ctx, status: str):
        if status.lower() == 'on': self.bot.autonomous_mode_enabled = True; await ctx.send("aight, vinny's off the leash.")
        elif status.lower() == 'off': self.bot.autonomous_mode_enabled = False; await ctx.send("thank god. vinny's back in his cage.")
        else: await ctx.send("it's 'on' or 'off', pal. pick one.")

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
        if not ctx.guild: return await ctx.send("can't clear memories from a private chat, pal.")
        path = constants.get_summaries_collection_path(self.bot.APP_ID, str(ctx.guild.id))
        if await self.bot.firestore_service.delete_docs(path): await ctx.send("aight, it's done. all the old chatter is gone.")
        else: await ctx.send("couldn't clear the memories. maybe they're stuck.")


async def setup(bot):
    await bot.add_cog(VinnyLogic(bot))