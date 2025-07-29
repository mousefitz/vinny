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
from typing import TYPE_CHECKING

from google.genai import types
from PIL import Image

# This block is only processed by type-checkers, not when the code runs.
if TYPE_CHECKING:
    from main import VinnyBot

class VinnyLogic(commands.Cog):
    def __init__(self, bot: 'VinnyBot'):
        self.bot = bot
        self.memory_scheduler.start()

    def cog_unload(self):
        self.memory_scheduler.cancel()

    async def update_vinny_mood(self):
        """Checks if enough time has passed and changes Vinny's mood."""
        if datetime.datetime.now() - self.bot.last_mood_change_time > self.bot.MOOD_CHANGE_INTERVAL:
            previous_mood = self.bot.current_mood
            new_mood = previous_mood
            while new_mood == previous_mood:
                new_mood = random.choice(self.bot.MOODS)
            self.bot.current_mood = new_mood
            self.bot.last_mood_change_time = datetime.datetime.now()
            sys.stderr.write(f"DEBUG: Vinny's mood has changed to '{self.bot.current_mood}'\n")

    async def find_and_tag_member(self, message, user_name: str):
        """Searches for a member and generates a creative, in-character response to tag them."""
        if not message.guild:
            await message.channel.send("eh, who am i supposed to tag out here? this is a private chat, pal.")
            return

        target_member = None
        lower_user_name = user_name.lower()
        
        for member in message.guild.members:
            if member.name.lower() == lower_user_name or \
               (member.nick and member.nick.lower() == lower_user_name) or \
               member.display_name.lower() == lower_user_name:
                target_member = member
                break
        
        if not target_member:
            for member in message.guild.members:
                if member.name.lower().startswith(lower_user_name) or \
                   (member.nick and member.nick.lower().startswith(lower_user_name)) or \
                   member.display_name.lower().startswith(lower_user_name):
                    target_member = member
                    break

        if not target_member:
            sys.stderr.write(f"DEBUG: No Discord name match for '{lower_user_name}'. Searching Vinny's memory...\n")
            target_member = await self.bot.find_user_by_vinny_name(message.guild, user_name)

        if target_member:
            response_text = f"aight, here they are: <@{target_member.id}>"
            if self.bot.API_CALL_COUNTS["text_generation"] < self.bot.TEXT_GENERATION_LIMIT:
                try:
                    tagging_prompt = (
                        f"{self.bot.personality_instruction}\n"
                        f"The user '{message.author.display_name}' asked you to tag the user '{target_member.display_name}'. You found them. "
                        f"Generate a short, in-character response to announce that you are tagging them. Be sassy, cranky, or flirty about it."
                    )
                    text_safety_settings = [
                        types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
                        for cat in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]
                    ]
                    api_response = await self.bot.gemini_client.aio.models.generate_content(
                        model=self.bot.MODEL_NAME,
                        contents=[types.Content(role='user', parts=[types.Part(text=tagging_prompt)])],
                        config=types.GenerateContentConfig(safety_settings=text_safety_settings)
                    )
                    if api_response.text:
                        response_text = f"{api_response.text.strip()} <@{target_member.id}>"
                    self.bot.API_CALL_COUNTS["text_generation"] += 1
                    await self.bot.update_api_count_in_firestore()
                except Exception as e:
                    sys.stderr.write(f"ERROR: Failed to generate creative tag comment: {e}\n")
            await message.channel.send(response_text)
        else:
            sys.stderr.write(f"DEBUG: Tag search for '{lower_user_name}' failed. Searched {len(message.guild.members)} members.\n")
            await message.channel.send(f"who? i looked all over this joint, couldn't find anyone named '{user_name}'.")

    @tasks.loop(minutes=30)
    async def memory_scheduler(self):
        await self.bot.wait_until_ready()
        sys.stderr.write("DEBUG: Memory scheduler starting...\n")
        if not self.bot.is_ready():
            sys.stderr.write("DEBUG: Bot not ready, skipping memory schedule.\n")
            return

        for guild in self.bot.guilds:
            messages_for_summary = []
            since_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
            
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).read_message_history:
                    try:
                        async for message in channel.history(limit=100, after=since_time, oldest_first=True):
                            if not message.author.bot:
                                messages_for_summary.append({
                                    "author": message.author.display_name,
                                    "content": message.content,
                                    "timestamp": message.created_at.isoformat()
                                })
                    except discord.Forbidden:
                        sys.stderr.write(f"Warning: No permission to read history in '{channel.name}'.\n")
                    except Exception as e:
                        sys.stderr.write(f"ERROR: Could not fetch history for channel '{channel.name}': {e}\n")

            if len(messages_for_summary) > 5:
                sys.stderr.write(f"DEBUG: Generating summary for guild '{guild.name}' with {len(messages_for_summary)} messages.\n")
                messages_for_summary.sort(key=lambda x: x['timestamp'])
                summary_data = await self.bot.generate_memory_summary(messages_for_summary)
                if summary_data:
                    await self.bot.save_memory(guild_id=str(guild.id), summary_data=summary_data, context_id=str(guild.id))
                    sys.stderr.write(f"DEBUG: Saved memory summary for guild '{guild.name}'.\n")
        sys.stderr.write("DEBUG: Memory scheduler finished.\n")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.id in self.bot.processed_message_ids:
            return
        
        if message.content.startswith(self.bot.command_prefix):
            return

        self.bot.processed_message_ids[message.id] = True

        try:
            bot_names = ["vinny", "vincenzo", "vin vin"]
            # Handle Replies
            if message.reference and (self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in bot_names)):
                try:
                    replied_to_message = await message.channel.fetch_message(message.reference.message_id)
                    prompt_parts = []
                    is_multimodal_reply = False
                    if replied_to_message.attachments and "image" in replied_to_message.attachments[0].content_type:
                        is_multimodal_reply = True
                        attachment = replied_to_message.attachments[0]
                        image_bytes = await attachment.read()
                        prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=image_bytes)))
                        prompt_parts.append(types.Part(text=(f"The user '{message.author.display_name}' replied with: \"{message.content}\"\nThey are replying to an older message from '{replied_to_message.author.display_name}' which contained the image above and said: \"{replied_to_message.content}\"\n\nYour task is to respond to the IMAGE in the OLDER message.")))
                    else:
                        prompt_parts.append(types.Part(text=(f"You are responding to a specific reply. The user '{message.author.display_name}' replied with: \"{message.content}\"\nThey are replying to an older message from '{replied_to_message.author.display_name}' which said: \"{replied_to_message.content}\"\n\nYour task is to respond to the OLDER message's content.")))

                    await self.update_vinny_mood()
                    dynamic_persona_injection = f"right now, your current mood is '{self.bot.current_mood}'."
                    
                    text_safety_settings = [types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE) for cat in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]]
                    full_safety_settings = [types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE) for cat in types.HarmCategory]
                    final_safety_settings = full_safety_settings if is_multimodal_reply else text_safety_settings
                    
                    final_reply_prompt_parts = [types.Part(text=f"{self.bot.personality_instruction}\n{dynamic_persona_injection}\n\n"), *prompt_parts]
                    
                    async with message.channel.typing():
                        response = await self.bot.gemini_client.aio.models.generate_content(
                            model=self.bot.MODEL_NAME,
                            contents=[types.Content(role='user', parts=final_reply_prompt_parts)],
                            config=types.GenerateContentConfig(safety_settings=final_safety_settings)
                        )
                        if response.text and response.text.strip():
                            for chunk in self.bot.split_message(response.text):
                                await message.channel.send(chunk.lower())
                                await asyncio.sleep(random.uniform(1.0, 1.5))
                    return
                except discord.NotFound:
                    sys.stderr.write("Warning: Could not find replied-to message.\n")
                except Exception as e:
                    sys.stderr.write(f"ERROR: Failed to handle reply: {e}\n")

            await self.update_vinny_mood()
            utc_now = datetime.datetime.now(datetime.UTC)
            local_now = utc_now.astimezone(ZoneInfo("America/New_York"))
            time_of_day_comment = "it's the afternoon."
            if 5 <= local_now.hour < 12: time_of_day_comment = "it's the morning, so you're feeling groggy."
            elif 18 <= local_now.hour < 22: time_of_day_comment = "it's the evening, a good time for a drink."
            else: time_of_day_comment = "it's late at night, and your thoughts are extra scattered."
            
            # Name Saving Logic
            name_patterns = [r"my name is\s+([A-Z][a-z]{2,})", r"call me\s+([A-Z][a-z]{2,})", r"you can call me\s+([A-Z][a-z]{2,})", r"i'm\s+([A-Z][a-z]{2,})", r"i am\s+([A-Z][a-z]{2,})"]
            for pattern in name_patterns:
                match = re.search(pattern, message.content)
                if match:
                    nickname = match.group(1)
                    if nickname.lower() not in ['vinny', 'vincenzo', 'vin']:
                        if await self.bot.save_user_nickname(str(message.author.id), nickname):
                            await message.channel.send(f"aight, {nickname}, vinny's got it. maybe.")
                        else:
                            await message.channel.send("my head's spinnin'. tried to remember that.")
                        return

            effective_message_content = message.content
            prompt_parts = [types.Part(text=effective_message_content)]
            is_multimodal = False
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and "image" in attachment.content_type:
                        is_multimodal = True
                        image_bytes = await attachment.read()
                        prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=image_bytes)))
            
            message_content_lower = effective_message_content.lower()
            
            # Reaction Logic
            explicit_reaction_keywords = ["react to this", "add an emoji", "emoji this", "react vinny"]
            if "pie" in message_content_lower and random.random() < 0.75:
                await message.add_reaction('🥧')
            elif any(keyword in message_content_lower for keyword in explicit_reaction_keywords):
                try:
                    if message.guild and message.guild.emojis: emoji = random.choice(message.guild.emojis)
                    else: emoji = random.choice(['😂', '👍', '👀', '🍕', '🍻', '🥃', '🐶', '🎨'])
                    await message.add_reaction(emoji)
                except Exception as e:
                    sys.stderr.write(f"ERROR: Failed to add explicit reaction: {e}\n")
            elif random.random() < self.bot.reaction_chance and not self.bot.user.mentioned_in(message):
                try:
                    if message.guild and message.guild.emojis: emoji = random.choice(message.guild.emojis)
                    else: emoji = random.choice(['😂', '👍', '👀', '🍕', '🍻', '🥃', '🐶', '🎨'])
                    await message.add_reaction(emoji)
                except Exception as e:
                    sys.stderr.write(f"ERROR: Failed to add random reaction: {e}\n")

            cleaned_for_triggers = message_content_lower.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
            for name in bot_names:
                if cleaned_for_triggers.startswith(name):
                    cleaned_for_triggers = cleaned_for_triggers[len(name):].strip(" ,:;")
                    break
            
            # --- Smart Image Prompt Generation ---
            image_trigger_keywords = ["paint", "draw", "make a picture of", "create an image of", "generate an image of"]
            image_prompt = ""
            for keyword in image_trigger_keywords:
                if cleaned_for_triggers.startswith(keyword):
                    image_prompt = cleaned_for_triggers[len(keyword):].strip()
                    break
            if image_prompt:
                async with message.channel.typing():
                    # --- Step 1: Dynamically generate a "thinking" message ---
                    thinking_message = "aight, lemme get my brushes..." # Fallback message
                    try:
                        thinking_prompt = (
                            f"You are Vinny, an eccentric artist. A user just asked you to paint '{image_prompt}'.\n"
                            f"Generate a very short, in-character phrase (in lowercase with typos) that you would say as you're about to start painting.\n"
                            f"Do not repeat the user's prompt. Examples: 'another masterpiece comin right up...', 'hmmm this one's gonna take some inspiration... and wine', 'aight aight i hear ya...'"
                        )
                        text_safety_settings = [types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE) for cat in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]]
                        
                        thinking_response = await self.bot.gemini_client.aio.models.generate_content(
                            model=self.bot.MODEL_NAME,
                            contents=[types.Content(role='user', parts=[types.Part(text=thinking_prompt)])],
                            config=types.GenerateContentConfig(safety_settings=text_safety_settings)
                        )
                        if thinking_response.text:
                            thinking_message = thinking_response.text.strip()
                    except Exception as e:
                        sys.stderr.write(f"ERROR: Failed to generate dynamic thinking message: {e}\n")
                    
                    await message.channel.send(thinking_message)
                    
                    # --- Step 2: Create a meta-prompt to rewrite the user's prompt ---
                    prompt_rewriter_instruction = (
                        f"You are Vinny, an eccentric and chaotic artist. A user wants you to paint a picture. Their simple request is: '{image_prompt}'.\n"
                        f"Your task is to rewrite this simple request into a rich, detailed, and artistic prompt for an image generation AI. Infuse it with your personality.\n"
                        f"- **Style**: Describe the scene as a masterpiece painting, using terms like 'oil on canvas', 'dramatic lighting', 'vibrant colors', 'chaotic energy'.\n"
                        f"- **Negatives**: Crucially, if the user asks for something to be excluded (e.g., 'without flowers'), you MUST add strong negative prompts like 'no flowers', 'devoid of flowers', 'barren of floral elements'.\n"
                        f"- **Persona**: If it doesn't contradict the user, consider adding elements of your world: your dogs, your messy garden, a bottle of wine, a slice of pizza.\n"
                        f"The final rewritten prompt should be a single, descriptive paragraph. Do not write any other text."
                    )
                    
                    # --- Step 3: Call the LLM to get the smarter prompt ---
                    try:
                        rewritten_prompt_response = await self.bot.gemini_client.aio.models.generate_content(
                            model=self.bot.MODEL_NAME,
                            contents=[types.Content(role='user', parts=[types.Part(text=prompt_rewriter_instruction)])],
                            config=types.GenerateContentConfig(safety_settings=text_safety_settings)
                        )
                        smarter_prompt = rewritten_prompt_response.text.strip()
                        sys.stderr.write(f"DEBUG: Original prompt: '{image_prompt}' | Rewritten prompt: '{smarter_prompt}'\n")
                    except Exception as e:
                        sys.stderr.write(f"ERROR: Failed to rewrite image prompt, using original. Error: {e}\n")
                        smarter_prompt = image_prompt
                    
                    # --- Step 4: Generate the image using the smarter prompt ---
                    image_file = await self.bot.generate_image_with_imagen(smarter_prompt)
                    
                    if image_file:
                        # --- Step 5: Generate a creative comment about the finished art ---
                        response_text = "here, i made this for ya." # Fallback text
                        try:
                            comment_prompt = (
                                f"You are Vinny, an eccentric artist. You just finished painting a picture based on the user's request for '{image_prompt}'.\n"
                                f"Generate a short, single-paragraph response to show them your work. Be chaotic, funny, or complain about it, in your typical lowercase, typo-ridden style.\n"
                                f"DO NOT repeat the original prompt '{image_prompt}' in your response."
                            )
                            comment_response = await self.bot.gemini_client.aio.models.generate_content(
                                model=self.bot.MODEL_NAME, contents=[types.Content(role='user', parts=[types.Part(text=comment_prompt)])],
                                config=types.GenerateContentConfig(safety_settings=text_safety_settings))
                            if comment_response.text:
                                response_text = comment_response.text.strip()
                        except Exception as e:
                            sys.stderr.write(f"ERROR: Failed to generate creative image comment: {e}\n")
                        
                        await message.channel.send(response_text, file=discord.File(image_file, filename="vinny_masterpiece.png"))
                    else:
                        await message.channel.send("ah, crap. vinny's hands are a bit shaky today. the thing came out all wrong.")
                return

            # Tagging Trigger
            tag_trigger_keywords = ["tag", "mention", "ping"]
            target_user_name = ""
            for keyword in tag_trigger_keywords:
                if cleaned_for_triggers.startswith(keyword):
                    target_user_name = cleaned_for_triggers[len(keyword):].strip()
                    break
            if target_user_name:
                await self.find_and_tag_member(message, target_user_name)
                return
            
            # Name Recall Trigger
            name_recall_triggers = ["what's my name", "what is my name", "do you know my name", "tell me my name"]
            if any(trigger in message_content_lower for trigger in name_recall_triggers):
                user_id = str(message.author.id)
                nickname = await self.bot.get_user_nickname(user_id)
                if nickname: await message.channel.send(f"your name? uh... vinny's head is fuzzy... but i think they call ya {nickname}, right?")
                else: await message.channel.send("your name? nah, i got nothin'. you never told me your name, pal.")
                return

            # Text Response Logic
            response_trigger = None
            if self.bot.user.mentioned_in(message) or any(name in message_content_lower for name in bot_names): response_trigger = "direct"
            elif self.bot.autonomous_mode_enabled or message.guild is None: response_trigger = "autonomous_always"
            elif random.random() < self.bot.autonomous_reply_chance: response_trigger = "autonomous_random"

            if not response_trigger: return

            if self.bot.API_CALL_COUNTS["text_generation"] >= self.bot.TEXT_GENERATION_LIMIT:
                await message.channel.send("whoa there, pal. vinny's brain is fried.")
                return

            lock = self.bot.channel_locks.setdefault(str(message.channel.id), asyncio.Lock())
            async with lock:
                async with message.channel.typing():
                    user_id, guild_id = str(message.author.id), str(message.guild.id) if message.guild else None
                    user_profile = await self.bot.get_user_profile(user_id, guild_id)
                    profile_facts, relationship_status = [], "neutral"
                    if user_profile:
                        if 'relationship_status' in user_profile: relationship_status = user_profile.pop('relationship_status')
                        for key, value in user_profile.items(): profile_facts.append(f"{key.replace('_', ' ')} is {value}")
                    profile_facts_string = ", ".join(profile_facts) if profile_facts else "You don't know anything specific about them."
                    
                    history = [
                        types.Content(role='user', parts=[types.Part(text=self.bot.personality_instruction)]),
                        types.Content(role='model', parts=[types.Part(text="aight, i get it. i'm vinny.")])
                    ]
                    async for msg in message.channel.history(limit=self.bot.MAX_CHAT_HISTORY_LENGTH):
                        if msg.id == message.id: continue
                        role = "model" if msg.author == self.bot.user else "user"
                        history.append(types.Content(role=role, parts=[types.Part(text=f"{msg.author.display_name}: {msg.content}")]))
                    history.reverse()

                    final_instruction_text = (
                        f"# --- YOUR CURRENT CONTEXT AND TASK ---\n"
                        f"- Your State: Your mood is {self.bot.current_mood}. The time is {time_of_day_comment}.\n"
                        f"- Context on User: Replying to {message.author.display_name}. Your relationship is {relationship_status}. Facts: {profile_facts_string}\n"
                        f"Based on all context, respond to the message from '{message.author.display_name}'. Obey all Directives.\n---"
                    )
                    final_prompt_parts = [types.Part(text=final_instruction_text), *prompt_parts]
                    history.append(types.Content(role='user', parts=final_prompt_parts))
                    
                    should_use_search, tools = False, []
                    question_words = ["who is", "what is", "where is", "when is", "how is"]
                    if "?" in effective_message_content or any(word in message_content_lower for word in question_words):
                        if self.bot.API_CALL_COUNTS["search_grounding"] < self.bot.SEARCH_GROUNDING_LIMIT:
                            should_use_search, self.bot.API_CALL_COUNTS["search_grounding"] = True, self.bot.API_CALL_COUNTS["search_grounding"] + 1
                    if should_use_search:
                        sys.stderr.write("DEBUG: Grounding with Google Search enabled.\n")
                        tools = [types.Tool(google_search=types.GoogleSearch())]

                    self.bot.API_CALL_COUNTS["text_generation"] += 1
                    await self.bot.update_api_count_in_firestore()

                    text_safety_settings = [types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE) for cat in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]]
                    full_safety_settings = [types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE) for cat in types.HarmCategory]
                    final_safety_settings = full_safety_settings if is_multimodal else text_safety_settings
                    
                    config = types.GenerateContentConfig(safety_settings=final_safety_settings, tools=tools)
                    response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)
                    
                    while response.candidates and response.candidates[0].content.parts[0].function_call:
                        function_call = response.candidates[0].content.parts[0].function_call
                        if function_call.name == 'Google Search':
                            tool_response = self.bot.gemini_client.tools.google_search(function_call.args)
                            function_response_part = types.Part(function_response=types.FunctionResponse(name='Google Search', response={'result': tool_response}))
                            history.append(response.candidates[0].content)
                            history.append(types.Content(parts=[function_response_part]))
                            response = await self.bot.gemini_client.aio.models.generate_content(model=self.bot.MODEL_NAME, contents=history, config=config)
                        else: break

                    if response.prompt_feedback and response.prompt_feedback.block_reason:
                        sys.stderr.write(f"API call blocked. Reason: {response.prompt_feedback.block_reason.name}")
                        await message.channel.send("whoa there, pal. vinny's brain is fried or somethin'.")
                        return

                    raw_response_text = response.text
                    if raw_response_text and raw_response_text.strip().lower() != '[silence]':
                        message_chunks, total_chars_sent, MAX_RESPONSE_CHARS = self.bot.split_message(raw_response_text), 0, 750
                        for chunk in message_chunks:
                            if total_chars_sent + len(chunk) > MAX_RESPONSE_CHARS:
                                remaining_chars = MAX_RESPONSE_CHARS - total_chars_sent
                                if remaining_chars > 20: await message.channel.send(chunk[:remaining_chars].rsplit(' ', 1)[0] + "...")
                                break
                            await message.channel.send(chunk.lower())
                            total_chars_sent += len(chunk)
                            await asyncio.sleep(random.uniform(1.0, 1.5))

                    if self.bot.PASSIVE_LEARNING_ENABLED and not image_prompt:
                        extracted_facts = await self.bot.extract_facts_from_message(effective_message_content)
                        if extracted_facts:
                            for key, value in extracted_facts.items():
                                await self.bot.save_user_profile_fact(user_id, guild_id, key, value)
        except Exception as e:
            sys.stderr.write(f"CRITICAL ERROR in on_message: {type(e).__name__}: {e}\n")
            await message.channel.send("oops! vinny's brain got a little fuzzy...")

    # --- ALL COMMANDS ---
    @commands.command(name='help')
    async def help_command(self, ctx):
        embed = discord.Embed(
            title="What do ya want?",
            description="Heh. Aight, so you need help? Pathetic. Here's the stuff I can do if ya use the '!' thing. Don't get used to it.",
            color=discord.Color.dark_gold()
        )
        embed.add_field(name="!remember [text]", value="Tells me to remember somethin'. I'll probably forget.\n*Example: `!remember my dog is named fido`*", inline=False)
        embed.add_field(name="!recall [topic]", value="Tries to remember somethin' we talked about *in this specific place*.\n*Example: `!recall fido`*", inline=False)
        embed.add_field(name="!vinnyknows [fact]", value="Teaches me somethin' about you, like `my [key] is [value]`.\n*Example: `!vinnyknows my favorite color is blue`*", inline=False)
        embed.add_field(name="!weather [location]", value="Gives you the damn weather. Don't blame me if it's wrong.\n*Example: `!weather 90210`*", inline=False)
        if await self.bot.is_owner(ctx.author):
            embed.add_field(name="!autonomy [on/off]", value="**(Owner Only)** Turns my brain on or off. Lets me talk without bein' talked to. Or shuts me up.", inline=False)
            embed.add_field(name="!set_relationship [@user] [type]", value="**(Owner Only)** Sets my feelings about someone. Types are: `friends`, `rivals`, `distrusted`, `admired`, `annoyance`, `neutral`.", inline=False)
            embed.add_field(name="!clear_memories", value="**(Owner Only)** Clears all of my automatic conversation summaries for this server.", inline=False)
        embed.set_footer(text="Now stop botherin' me. Salute!")
        await ctx.send(embed=embed)

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

    @commands.command(name='remember')
    async def remember_command(self, ctx, *, thing_to_remember: str):
        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id) if ctx.guild else None
        channel_id = str(ctx.channel.id)
        success = await self.bot.save_explicit_memory(user_id, guild_id, channel_id, thing_to_remember)
        if success:
            await ctx.send(f"alright, {ctx.author.mention}. vinny's got that tucked away in his hazy brain. 'bout '{thing_to_remember}', eh? i'll try not to forget it... at least not while we're here.")
        else:
            await ctx.send(f"uh oh, {ctx.author.mention}. vinny tried to remember that, but his head's spinnin'.")
    
    @commands.command(name='recall')
    async def recall_command(self, ctx, *, topic: str):
        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id) if ctx.guild else None
        await ctx.send(f"lemme see. {ctx.author.mention} wants to know about '{topic}', eh? vinny's tryin' to remember what we talked about *here*.")
        response_message = await self.bot.retrieve_explicit_memories(user_id, guild_id, topic)
        await ctx.channel.send(response_message)
        
    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        match = re.search(r'^(my\s+|their\s+|his\s+|her\s+)?(.*?)\s+is\s+(.*)$', knowledge_string, re.IGNORECASE)
        if not match:
            await ctx.send("eh? what're you tryin' to tell me? say it like this: `!vinnyknows my favorite food is pizza`.")
            return
        key, value = match.group(2).strip(), match.group(3).strip()
        if not key or not value:
            await ctx.send("you gotta give me a thing and what it is. vinny's not a mind reader.")
            return
        guild_id = str(ctx.guild.id) if ctx.guild else None
        success = await self.bot.save_user_profile_fact(str(ctx.author.id), guild_id, key, value)
        if success:
            await ctx.send(f"aight, i got it. so '{key}' is '{value}'. vinny will... probably remember that.")
        else:
            await ctx.send("my head's all fuzzy. tried to remember that but it slipped away.")

    @commands.command(name='autonomy')
    @commands.is_owner()
    async def autonomy_command(self, ctx, status: str = None):
        if status is None:
            current_status = "on" if self.bot.autonomous_mode_enabled else "off"
            await ctx.send(f"gimme a break, {ctx.author.mention}. the autonomy thing is currently {current_status}.")
            return
        status_lower = status.lower()
        if status_lower == 'on':
            self.bot.autonomous_mode_enabled = True
            await ctx.send(f"aight, fine, {ctx.author.mention}. vinny's off the leash. no tellin' what'll happen now.")
        elif status_lower == 'off':
            self.bot.autonomous_mode_enabled = False
            await ctx.send(f"thank god. vinny's back in his cage. was gettin' tired of thinkin' for myself.")
        else:
            await ctx.send(f"on or off, pal. what's so hard about that?")

    @commands.command(name='set_relationship')
    @commands.is_owner()
    async def set_relationship_command(self, ctx, member: discord.Member, relationship_type: str):
        valid_relationships = ['friends', 'rivals', 'distrusted', 'admired', 'annoyance', 'neutral']
        rel_type = relationship_type.lower()
        if rel_type not in valid_relationships:
            await ctx.send(f"that ain't a real relationship type. try one of these: {', '.join(valid_relationships)}")
            return
        success = await self.bot.save_user_profile_fact(str(member.id), str(ctx.guild.id), 'relationship_status', rel_type)
        if success:
            await ctx.send(f"aight, got it. from now on, me and {member.display_name} are... '{rel_type}'.")
        else:
            await ctx.send("my brain's all fuzzy. couldn't lock that in.")

    @commands.command(name='clear_memories')
    @commands.is_owner()
    async def clear_memories_command(self, ctx):
        if not ctx.guild:
            await ctx.send("this only works in a server, pal.")
            return
        await ctx.send(f"aight, hold on. tryin' to wipe the slate clean for this server: **{ctx.guild.name}**...")
        summaries_ref = self.bot.db.collection(f"artifacts/{self.bot.APP_ID}/servers/{str(ctx.guild.id)}/summaries")
        success = await self.bot.delete_docs_from_firestore(summaries_ref)
        if success:
            await ctx.send("aight, it's done. all the old chatter from this place is gone from my head.")
        else:
            await ctx.send("bah, my head's fuzzy. somethin' went wrong.")

async def setup(bot):
    await bot.add_cog(VinnyLogic(bot))