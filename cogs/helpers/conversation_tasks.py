import asyncio
import logging
import re
import json
import discord
from google.genai import types
from . import ai_classifiers, utilities
from utils import constants
from bs4 import BeautifulSoup
from utils import api_clients
from readability import Document

async def get_keywords_for_memory_search(bot_instance, text: str):
    """
    Extracts semantic keywords using AI, with a Regex fallback for speed/safety.
    """
    # 1. Quick length check to save money
    if len(text) < 10:
        return []

    # 2. Try the SMART way (AI)
    prompt = f"Extract 3-5 key topics or keywords from this text for memory search: '{text}'. Return as a comma-separated list."
    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[prompt],
            config=bot_instance.GEMINI_TEXT_CONFIG
        )
        if response and response.text:
            keywords = [k.strip() for k in response.text.split(',')]
            return keywords
            
    except Exception as e:
        logging.error(f"AI Keyword Extraction failed: {e}")
    
    # 3. Fallback to the CHEAP way (Regex) if AI fails
    # This ensures Vinny never crashes just because the API hiccuped.
    ignore_words = {"the", "and", "is", "it", "to", "in", "of", "that", "this", "for", "with", "you", "me", "vinny"}
    words = re.findall(r'\w+', text.lower())
    keywords = [w for w in words if w not in ignore_words and len(w) > 3]
    return keywords[:3]

async def handle_direct_reply(bot_instance, message: discord.Message):
    """Handles a direct reply (via reply or mention) to one of the bot's messages OR another user's image."""
    
    replied_to_message = None
    if message.reference and message.reference.message_id:
        try:
            replied_to_message = await message.channel.fetch_message(message.reference.message_id)
        except: pass
    else:
        async for prior_message in message.channel.history(limit=10):
            if prior_message.author == bot_instance.user:
                replied_to_message = prior_message
                break
    
    if not replied_to_message:
        await handle_text_or_image_response(bot_instance, message, is_autonomous=False)
        return

    user_id = str(message.author.id)
    guild_id = str(message.guild.id) if message.guild else None

    # --- 1. GET FULL MEMORY & TONE ---
    user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
    rel_score = user_profile.get("relationship_score", 0)
    facts = user_profile.get("facts", {})
    facts_str = "\n".join([f"- {k}: {v}" for k, v in facts.items()]) if facts else "No specific facts remembered yet."

    # Dynamic Tone
    if rel_score >= 60: tone_instruction = "## RELATIONSHIP: BEST FRIEND (High Trust)\n- You LOVE this user. Be warm, enthusiastic, and loyal.\n- Use nicknames like 'pal', 'buddy', 'boss'."
    elif rel_score >= 10: tone_instruction = "## RELATIONSHIP: FRIEND (Good Terms)\n- You like this user. Be casual and friendly."
    elif rel_score >= -10: tone_instruction = "## RELATIONSHIP: NEUTRAL\n- You don't know this user well. Be guarded and indifferent."
    elif rel_score >= -60: tone_instruction = "## RELATIONSHIP: DISLIKED (Suspicious)\n- You DO NOT trust this user. Be short, dismissive, and sarcastic.\n- If they ask for something, complain."
    else: tone_instruction = "## RELATIONSHIP: ENEMY (Hostile)\n- You HATE this user. Be openly rude, mocking, and aggressive.\n- Tell them to get lost."

    # --- 2. RELATIONSHIP SCORING ---
    if len(message.content) > 3:
        impact_score = await ai_classifiers.analyze_sentiment_impact(
            bot_instance, message.author.display_name, message.content
        )
        if impact_score != 0:
            asyncio.create_task(bot_instance.firestore_service.update_relationship_score(user_id, guild_id, impact_score))

    user_name_to_use = await bot_instance.firestore_service.get_user_nickname(user_id) or message.author.display_name
    
    # --- 3. BUILD THE PROMPT SCRIPT ---
    chat_transcript = ""
    async for msg in message.channel.history(limit=5, before=message):
        chat_transcript = f"{msg.author.display_name}: {msg.content}\n" + chat_transcript

    reply_prompt_text = (
        f"## YOUR CURRENT STATE:\n"
        f"- Mood: {bot_instance.current_mood}\n"
        f"{tone_instruction}\n\n"
        f"## KNOWN FACTS ABOUT {user_name_to_use.upper()}:\n{facts_str}\n\n"
        f"## RECENT CHAT TRANSCRIPT:\n{chat_transcript}\n"
        f"--- END TRANSCRIPT ---\n\n"
        f"## CONTEXT OF THE DIRECT REPLY:\n"
        f"You previously said: \"{replied_to_message.content}\"\n"
        f"The user '{user_name_to_use}' has directly replied to THAT specific message with: \"{message.content}\"\n\n"
        f"## YOUR TASK:\n"
        f"Generate a short, in-character response to their reply. \n"
        f"CRITICAL RULES:\n"
        f"1. **DO NOT REPEAT** the user's message back to them.\n"
        f"2. If looking at an image, comment on it specifically."
    )

    # --- 4. HANDLE IMAGES (The Eyes) ---
    api_parts = []
    if replied_to_message.attachments:
        for att in replied_to_message.attachments:
            if "image" in att.content_type:
                try:
                    image_bytes = await att.read()
                    if len(image_bytes) < 8 * 1024 * 1024:
                        api_parts.append(types.Part.from_bytes(data=image_bytes, mime_type=att.content_type))
                        reply_prompt_text = "[SYSTEM: The user is replying to the image attached above.]\n" + reply_prompt_text
                        break
                except Exception as e: logging.error(f"Failed to attach context image: {e}")

    if message.attachments:
         for att in message.attachments:
            if "image" in att.content_type:
                try:
                    image_bytes = await att.read()
                    if len(image_bytes) < 8 * 1024 * 1024:
                        api_parts.append(types.Part.from_bytes(data=image_bytes, mime_type=att.content_type))
                        reply_prompt_text = "[SYSTEM: The user sent the image attached above.]\n" + reply_prompt_text
                        break
                except Exception as e: logging.error(f"Failed to attach current image: {e}")

    api_parts.append(types.Part(text=reply_prompt_text))

    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME, contents=[types.Content(role="user", parts=api_parts)], config=bot_instance.GEMINI_TEXT_CONFIG
        )
        if response and response.text:
            cleaned_response = response.text.strip()
            if cleaned_response and cleaned_response.lower() != '[silence]':
                for chunk in bot_instance.split_message(cleaned_response):
                    await message.channel.send(chunk.lower())
        else: await message.channel.send("huh? sorry i spaced out for a second.")
    except Exception: await message.channel.send("my brain just shorted out for a second.")

async def handle_text_or_image_response(bot_instance, message: discord.Message, is_autonomous: bool = False, summary: str = ""):
    """Core logic for generating a text response based on chat history."""
    async with bot_instance.channel_locks.setdefault(str(message.channel.id), asyncio.Lock()):
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else None
        user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
        rel_score = user_profile.get("relationship_score", 0)
        facts = user_profile.get("facts", {})
        facts_str = "\n".join([f"- {k}: {v}" for k, v in facts.items()]) if facts else "No specific facts remembered yet."

        # 🧠 TONE SELECTOR
        tone_instruction = "## RELATIONSHIP: NEUTRAL\n- You don't know this user well. Be guarded and indifferent."
        if rel_score >= 60:   # Bestie / Worshipped
            tone_instruction = "## RELATIONSHIP: BEST FRIEND (High Trust)\n- You LOVE this user. Be warm, enthusiastic, and loyal.\n- Use nicknames like 'pal', 'buddy', 'boss'."
        elif rel_score >= 10: # Friend / Chill
            tone_instruction = "## RELATIONSHIP: FRIEND (Good Terms)\n- You like this user. Be casual and friendly."
        elif rel_score >= -10: # Neutral
            tone_instruction = "## RELATIONSHIP: NEUTRAL\n- You don't know this user well. Be guarded and indifferent."
        elif rel_score >= -60: # Annoyance / Sketchy
            tone_instruction = "## RELATIONSHIP: DISLIKED (Suspicious)\n- You DO NOT trust this user. Be short, dismissive, and sarcastic.\n- If they ask for something, complain."
        else: # Enemy / Nemesis
            tone_instruction = "## RELATIONSHIP: ENEMY (Hostile)\n- You HATE this user. Be openly rude, mocking, and aggressive.\n- Tell them to get lost."

        custom_nickname = await bot_instance.firestore_service.get_user_nickname(user_id)
        actual_display_name = custom_nickname if custom_nickname else message.author.display_name

        # --- SENTIMENT & TOPIC SCORING ---
        if len(message.content) > 3:
            impact_score = await ai_classifiers.analyze_sentiment_impact(
                bot_instance, message.author.display_name, message.content
            )
            if impact_score != 0:
                new_score = await bot_instance.firestore_service.update_relationship_score(
                    user_id, guild_id, impact_score
                )
                await update_relationship_status(bot_instance, user_id, guild_id, new_score)
                if impact_score > 0: logging.info(f"📈 {message.author.display_name} gained {impact_score} pts. Total: {new_score:.2f}")
                else: logging.info(f"📉 {message.author.display_name} lost {impact_score} pts. Total: {new_score:.2f}")

        # --- MEMORY INJECTION ---
        relevant_memories_text = ""
        if message.guild:
            keywords = await get_keywords_for_memory_search(bot_instance, message.content)
            if keywords:
                found_memories = await bot_instance.firestore_service.retrieve_relevant_memories(
                    str(message.guild.id), keywords, limit=2
                )
                if found_memories:
                    memory_strings = []
                    for mem in found_memories:
                        date_str = mem.get("timestamp", "the past")
                        if hasattr(date_str, "strftime"): date_str = date_str.strftime("%Y-%m-%d")
                        memory_strings.append(f"- [{date_str}]: {mem.get('summary')}")
                    relevant_memories_text = "\n".join(memory_strings)

        # --- 1. DETERMINE SPECIFIC INSTRUCTIONS (Triage / Autonomous) ---
        cleaned_content = re.sub(f'<@!?{bot_instance.user.id}>', '', message.content).strip()
        config = bot_instance.GEMINI_TEXT_CONFIG
        task_instruction = ""

        if "?" in message.content:
            question_type = await ai_classifiers.triage_question(bot_instance, cleaned_content)
            if question_type == "real_time_search":
                config = types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    safety_settings=bot_instance.GEMINI_TEXT_CONFIG.safety_settings,
                    max_output_tokens=bot_instance.GEMINI_TEXT_CONFIG.max_output_tokens,
                    temperature=bot_instance.GEMINI_TEXT_CONFIG.temperature
                )
                task_instruction = "CRITICAL TASK: The user asked a factual question. You MUST use the Google Search tool to find the answer. Provide the fact, then add a short in-character comment."
            elif question_type == "general_knowledge":
                task_instruction = "CRITICAL TASK: Answer the factual question accurately based on your internal knowledge, then add an in-character comment."
            else:
                task_instruction = "TASK: The user asked a personal question. Answer directly and honestly in character. Do not summarize the chat."
        elif is_autonomous:
            task_instruction = (
                "TASK: You are 'hanging out' in this chat server. Chime in naturally as if sitting on the couch with them.\n"
                "RULES:\n1. READ THE ROOM: Understand the topic of the chat history.\n2. ADD VALUE: Don't just say 'lol', add a joke or question.\n3. BE BRIEF."
            )
        else:
            task_instruction = (
                "TASK: Detect the conversation flow.\n"
                "1. CONNECT: If this message responds to the chat history, CONTINUE the topic.\n"
                "2. INTERPRET: If it's short (e.g., 'why?'), it refers to the previous message. Do not treat it as a standalone statement.\n"
                "3. RESPOND: Reply naturally. Do not repeat their message."
            )

        # --- 2. BUILD THE CHAT TRANSCRIPT (Short-Term Memory) ---
        chat_transcript = ""
        participants = set()
        async for msg in message.channel.history(limit=bot_instance.MAX_CHAT_HISTORY_LENGTH, before=message):
            if not msg.author.bot: participants.add(msg.author.display_name)
            
            # Add a marker if the past message had files attached
            attachment_note = ""
            if msg.attachments:
                attachment_note = f" [Attached {len(msg.attachments)} file(s)]"
                
            chat_transcript = f"{msg.author.display_name}: {msg.content}{attachment_note}\n" + chat_transcript
        
        participants.add(actual_display_name)
        participant_list = ", ".join(sorted(list(participants)))

        # --- 3. ASSEMBLE THE MASTER PROMPT ---
        master_prompt_text = (
            f"## YOUR CURRENT STATE:\n"
            f"- Mood: {bot_instance.current_mood}\n"
            f"{tone_instruction}\n\n"
        )
        
        if facts_str and facts_str != "No specific facts remembered yet.":
            master_prompt_text += f"## KNOWN FACTS ABOUT {actual_display_name.upper()}:\n{facts_str}\n\n"
            
        if relevant_memories_text:
            master_prompt_text += f"## RECALLED SERVER MEMORIES (Use if relevant):\n{relevant_memories_text}\n\n"

        master_prompt_text += (
            f"## CURRENT CONVERSATION TRANSCRIPT:\n"
            f"(The users talking are: [{participant_list}]. Correctly attribute all statements to who said them.)\n"
            f"{chat_transcript}\n"
            f"--- END TRANSCRIPT ---\n\n"
            f"## {task_instruction}\n\n"
            f"Now, reply to this new message from {actual_display_name}:\n"
            f"{actual_display_name}: {cleaned_content}"
        )

        # --- 4. PREPARE THE API CALL ---
        prompt_parts = [types.Part(text=master_prompt_text)]
        uploaded_media_file = None 

        if message.attachments:
            for attachment in message.attachments:
                if "image" in attachment.content_type:
                    image_bytes = await attachment.read()
                    prompt_parts.append(types.Part(inline_data=types.Blob(mime_type=attachment.content_type, data=image_bytes)))
                    break 
                elif "video" in attachment.content_type or "audio" in attachment.content_type:
                    async with message.channel.typing():
                        temp_filename = f"temp_{message.id}_{attachment.filename}"
                        await attachment.save(temp_filename)
                        try:
                            uploaded_media_file = await asyncio.to_thread(bot_instance.gemini_client.files.upload, path=temp_filename)
                            while uploaded_media_file.state.name == "PROCESSING":
                                await asyncio.sleep(1)
                                uploaded_media_file = await asyncio.to_thread(bot_instance.gemini_client.files.get, name=uploaded_media_file.name)
                            if uploaded_media_file.state.name == "FAILED": raise Exception("Media processing failed.")
                            prompt_parts.append(types.Part(file_data=types.FileData(file_uri=uploaded_media_file.uri, mime_type=uploaded_media_file.mime_type)))
                        except Exception as e:
                            logging.error(f"Failed to process media: {e}")
                            await message.channel.send("i tried to listen/watch that but my brain shorted out. bad file.")
                        finally:
                            import os
                            if os.path.exists(temp_filename): os.remove(temp_filename)
                    break

        # Send it as ONE unified message block
        history = [types.Content(role='user', parts=prompt_parts)]
        
        # ADD THIS: Show the typing indicator while Gemini thinks!
        async with message.channel.typing():
            response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=history, config=config)

        if uploaded_media_file:
            try: await asyncio.to_thread(bot_instance.gemini_client.files.delete, name=uploaded_media_file.name)
            except: pass 

        if response and response.text:
            cleaned_response = response.text.strip()
            
            if cleaned_response and cleaned_response.lower() != '[silence]':
                if is_autonomous:
                    typing_delay = min(len(cleaned_response) * 0.05, 8.0)
                    async with message.channel.typing():
                        await asyncio.sleep(typing_delay)
                        for chunk in bot_instance.split_message(cleaned_response):
                            if chunk: await message.channel.send(chunk.lower())
                else:
                    for chunk in bot_instance.split_message(cleaned_response):
                        if chunk: await message.channel.send(chunk.lower())
            elif cleaned_response.lower() == '[silence]':
                logging.info(f"Vinny decided to stay silent for message {message.id}")
        else:
            if response and response.candidates:
                logging.warning(f"⚠️ Empty Response! Finish Reason: {response.candidates[0].finish_reason}")
            else:
                logging.warning("⚠️ API returned None or no candidates.")

            if not is_autonomous:
                await message.channel.send("my brain just rebooted. what was that?")

async def handle_knowledge_request(bot_instance, message: discord.Message, target_user: discord.Member):
    """Retrieves facts about a user and generates a response."""
    user_id = str(target_user.id)
    guild_id = str(message.guild.id) if message.guild else None
    user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)

    if not user_profile:
        await message.channel.send(f"about {target_user.display_name}? i got nothin'. a blank canvas. kinda intimidatin', actually.")
        return

    facts_list = [f"- {key.replace('_', ' ')}: {value}" for key, value in user_profile.items()]
    facts_string = "\n".join(facts_list)
    
    summary_prompt = (
        f"# --- YOUR TASK ---\n"
        f"The user '{message.author.display_name}' has asked what you know about '{target_user.display_name}'. "
        f"Your only task is to summarize the facts listed below about **'{target_user.display_name}' ONLY**. "
        f"Do not mention or use any information about '{message.author.display_name}'. Respond in your unique, chaotic voice.\n\n"
        f"## FACTS I KNOW ABOUT {target_user.display_name}:\n"
        f"{facts_string}\n\n"
        f"## INSTRUCTIONS:\n"
        f"1.  Read ONLY the facts provided above about {target_user.display_name}.\n"
        f"2.  Weave them together into a short, lowercase, typo-ridden monologue about them.\n"
        f"3.  Do not just list the facts. Interpret them, connect them, or be confused by them in your own unique voice."
    )
    try:
        async with message.channel.typing():
            response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME, 
                contents=[summary_prompt], 
                config=bot_instance.GEMINI_TEXT_CONFIG
            )
            if response and response.text:
                await message.channel.send(response.text.strip())
            else:
                await message.channel.send("i know stuff about em, but i can't find the words. gimme a sec.")
    except Exception:
        logging.error("Failed to generate knowledge summary.", exc_info=True)
        await message.channel.send("my head's all fuzzy. i know some stuff but the words ain't comin' out right.")

async def handle_server_knowledge_request(bot_instance, message: discord.Message):
    """Retrieves conversation summaries and synthesizes them."""
    if not message.guild:
        await message.channel.send("what server? we're in a private chat, pal. my brain's fuzzy enough as it is.")
        return
    guild_id = str(message.guild.id)
    summaries = await bot_instance.firestore_service.retrieve_server_summaries(guild_id)
    if not summaries:
        await message.channel.send(f"this place? i ain't learned nothin' yet. it's all a blur. a beautiful, chaotic blur.")
        return
    formatted_summaries = "\n".join([f"- {s.get('summary', '...a conversation i already forgot.')}" for s in summaries])
    synthesis_prompt = (f"# --- YOUR TASK ---\nA user, '{message.author.display_name}', is asking what you've learned from overhearing conversations in this server. Your task is to synthesize the provided conversation summaries into a single, chaotic, and insightful monologue. Obey all your personality directives.\n\n## CONVERSATION SUMMARIES I'VE OVERHEARD:\n{formatted_summaries}\n\n## INSTRUCTIONS:\n1.  Read all the summaries to get a feel for the server's vibe.\n2.  Do NOT just list the summaries. Weave them together into a story or a series of scattered, in-character thoughts.\n3.  Generate a short, lowercase, typo-ridden response that shows what you've gleaned from listening in.")
    try:
        async with message.channel.typing():
            response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=[synthesis_prompt], config=bot_instance.GEMINI_TEXT_CONFIG)
            if response and response.text:
                await message.channel.send(response.text.strip())
            else:
                 await message.channel.send("i been listenin', but my memory just blanked. ask me again.")
    except Exception:
        logging.error("Failed to generate server knowledge summary.", exc_info=True)
        await message.channel.send("my head's a real mess. i've been listenin', but it's all just noise right now.")

async def handle_correction(bot_instance, message: discord.Message):
    """Identifies and removes MULTIPLE incorrect facts from the user's profile."""
    user_id = str(message.author.id)
    guild_id = str(message.guild.id) if message.guild else None
    
    # 1. Identify WHAT to remove (Allowing multiple items)
    correction_prompt = (
        f"A user is correcting facts about themselves. Their message is: \"{message.content}\".\n"
        f"Your task is to identify ALL the specific facts they are correcting.\n"
        f"Return a JSON object with a single key, \"facts_to_remove\", containing a LIST of strings.\n\n"
        f"Example:\n"
        f"User message: 'I'm not bald anymore and I hate pizza now.'\n"
        f"Output: {{\"facts_to_remove\": [\"is bald\", \"likes pizza\"]}}"
    )
    
    try:
        json_config = types.GenerateContentConfig(response_mime_type="application/json")
        async with message.channel.typing():
            # First API Call: Get the list of concepts
            response1 = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME, 
                contents=[correction_prompt], 
                config=json_config
            )
            
            if not response1 or not response1.text:
                await message.channel.send("my brain's all fuzzy, i didn't get what i was wrong about."); return
            
            clean_text1 = re.search(r'```json\s*(\{.*?\})\s*```', response1.text, re.DOTALL) or re.search(r'(\{.*?\})', response1.text, re.DOTALL)
            fact_data = json.loads(clean_text1.group(1)) if clean_text1 else {}
            facts_to_remove = fact_data.get("facts_to_remove", [])
            
            if not facts_to_remove:
                await message.channel.send("huh? what was i wrong about? try bein more specific, pal."); return
            
            # Fetch Profile
            user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
            if not user_profile:
                await message.channel.send("i don't even know anything about you to be wrong about!"); return
            
            # 2. Map concepts to Database Keys
            key_mapping_prompt = (
                f"A user wants to remove the following facts: {json.dumps(facts_to_remove)}.\n"
                f"I need to find the specific database keys in their profile that correspond to these facts.\n"
                f"Here is the user's current profile data: {json.dumps(user_profile, indent=2)}\n\n"
                f"## INSTRUCTIONS:\n"
                f"Return a JSON object with a key \"keys_to_delete\" containing a LIST of the exact database keys to remove.\n"
                f"If a fact doesn't have a matching key, ignore it."
            )
            
            # Second API Call: Get the DB Keys
            response2 = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME, 
                contents=[key_mapping_prompt], 
                config=json_config
            )
            
            if not response2 or not response2.text:
                await message.channel.send("i thought i knew somethin' but i can't find it in my brain. weird."); return
            
            clean_text2 = re.search(r'```json\s*(\{.*?\})\s*```', response2.text, re.DOTALL) or re.search(r'(\{.*?\})', response2.text, re.DOTALL)
            key_data = json.loads(clean_text2.group(1)) if clean_text2 else {}
            keys_to_delete = key_data.get("keys_to_delete", [])
            
            if not keys_to_delete:
                await message.channel.send("i looked through my notes but i couldn't find those specific facts recorded anywhere."); return
            
            # 3. Execute Deletions
            deleted_count = 0
            for key in keys_to_delete:
                if await bot_instance.firestore_service.delete_user_profile_fact(user_id, guild_id, key):
                    deleted_count += 1
            
            # 4. Confirmation Message
            if deleted_count > 0:
                if deleted_count == 1:
                    await message.channel.send(f"aight, my mistake. i'll forget that '{keys_to_delete[0].replace('_', ' ')}' thing.")
                else:
                    await message.channel.send(f"aight, i scrambled my brains. forgot {deleted_count} things about ya. fresh start.")
            else:
                await message.channel.send("tried to delete 'em, but my memory is stubborn. somethin' went wrong.")

    except Exception:
        logging.error("An error occurred in handle_correction.", exc_info=True)
        await message.channel.send("my head's poundin'. somethin went wrong tryin to fix my memory.")

async def find_and_tag_member(bot_instance, message, user_name: str, times: int = 1):
    """Finds a user in the server and 'tags' them with a message from Vinny."""
    MAX_TAGS = 5
    if times > MAX_TAGS:
        await message.channel.send(f"whoa there, buddy. {times} times? you tryna get me banned? i'll do it {MAX_TAGS} times, take it or leave it.")
        times = MAX_TAGS

    if not message.guild:
        await message.channel.send("eh, who am i supposed to tag out here? this is a private chat, pal.")
        return
    
    target_member = None
    match = re.match(r'<@!?(\d+)>', user_name)
    if match:
        user_id = int(match.group(1))
        target_member = message.guild.get_member(user_id)
    
    if not target_member:
        target_member = discord.utils.find(lambda m: user_name.lower() in m.display_name.lower(), message.guild.members)
    
    if not target_member:
        target_member = await utilities.find_user_by_vinny_name(bot_instance, message.guild, user_name)
    
    if target_member:
        try:
            original_command = message.content
            target_nickname = await bot_instance.firestore_service.get_user_nickname(str(target_member.id))
            
            name_info = f"Their display name is '{target_member.display_name}'."
            if target_nickname:
                name_info += f" You know them as '{target_nickname}'."

            tagging_prompt = (
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
            
            api_response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[tagging_prompt],
                config=bot_instance.GEMINI_TEXT_CONFIG
            )
            
            if api_response and api_response.text:
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

async def generate_memory_summary(bot_instance, messages):
    """Generates a summary and keywords for a list of messages."""
    if not messages or not bot_instance.firestore_service.db: return None
    summary_instruction = ("You are a conversation summarization assistant. Analyze the following conversation and provide a concise, one-paragraph summary. After the summary, provide a list of 3-5 relevant keywords. Your output must contain 'summary:' and 'keywords:' labels.")
    summary_prompt = f"{summary_instruction}\n\n...conversation:\n" + "\n".join([f"{msg['author']}: {msg['content']}" for msg in messages])
    try:
        response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=[summary_prompt], config=bot_instance.GEMINI_TEXT_CONFIG)
        
        if response and response.text:
            summary_match = re.search(r"summary:\s*(.*?)(keywords:|$)", response.text, re.DOTALL | re.IGNORECASE)
            keywords_match = re.search(r"keywords:\s*(.*)", response.text, re.DOTALL | re.IGNORECASE)
            summary = summary_match.group(1).strip() if summary_match else response.text.strip()
            keywords_raw = keywords_match.group(1).strip() if keywords_match else ""
            keywords = [k.strip() for k in keywords_raw.strip('[]').split(',') if k.strip()]
            return {"summary": summary, "keywords": keywords}
    except Exception:
        logging.error("Failed to generate memory summary.", exc_info=True)
    return None

async def update_relationship_status(bot_instance, user_id: str, guild_id: str | None, new_score: float):
    """Determines a user's relationship status based on their score."""
    # Use Centralized Logic
    new_status, _ = constants.get_relationship_status(new_score)
        
    current_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
    current_status = current_profile.get("relationship_status", "neutral")
    
    if current_status != new_status:
        await bot_instance.firestore_service.save_user_profile_fact(user_id, guild_id, "relationship_status", new_status)
        logging.info(f"Relationship status for user {user_id} changed from '{current_status}' to '{new_status}' (Score: {new_score:.2f})")

async def summarize_url(bot_instance, http_session, url): # Renamed to bot_instance
    """
    Ultra-Robust summarizer with a Googlebot fallback and Archive API check.
    """
    logging.info(f"--- 🌐 SUMMARIZATION START: {url} ---")
    
    attempts = [
        {
            "name": "Desktop Chrome",
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Referer": "https://www.google.com/"
            }
        },
        {
            "name": "Googlebot (The Skeleton Key)", # Many news sites unlock for Googlebot
            "headers": {
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
        }
    ]

    html = None
    for i, attempt in enumerate(attempts):
        try:
            logging.info(f"Attempt {i+1} ({attempt['name']})...")
            async with http_session.get(url, headers=attempt['headers'], timeout=12) as response:
                if response.status == 200:
                    html = await response.text()
                    logging.info(f"✅ Success with {attempt['name']}")
                    break
                logging.warning(f"❌ {attempt['name']} got {response.status}")
        except Exception as e:
            logging.error(f"Attempt {i+1} error: {e}")

    # ARCHIVE FALLBACK (Fixed URL format)
    if not html:
        logging.info("Trying Wayback Availability API...")
        api_url = f"http://archive.org/wayback/available?url={url}"
        try:
            async with http_session.get(api_url, timeout=10) as resp:
                data = await resp.json()
                if snapshots := data.get("archived_snapshots"):
                    closest = snapshots.get("closest")
                    if closest and closest.get("available"):
                        async with http_session.get(closest["url"]) as archive_resp:
                            html = await archive_resp.text()
                            logging.info("📜 Success via Wayback Machine!")
        except Exception as e:
            logging.error(f"Archive fallback failed: {e}")

    if not html: return "i tried every disguise, but that site's got a restraining order against me."

    # Process with Readability & Gemini (Keep your existing extraction/AI logic here)
    try:
        doc = Document(html)
        title = doc.title()
        soup = BeautifulSoup(doc.summary(), 'html.parser')
        clean_text = ' '.join(soup.get_text(separator=' ').split())
        
        if len(clean_text) < 200: return "got the page, but it's empty. maybe a login wall?"

        prompt = (
            f"# TASK:\n"
            f"Summarize this article titled '{title}' in your specific voice:\n\n{clean_text[:25000]}"
        )
        # Pass bot_instance to the generator so it tracks the cost!
        summary = await api_clients.generate_text_with_genai(bot_instance, prompt)
        return f"**{title}**\n\n{summary}" if summary else "brain fog. couldn't summarize."
    except Exception:
        return "the page code is a mess, i can't make sense of it."
