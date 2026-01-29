import asyncio
import logging
import re
import json
import discord
from google.genai import types
from . import ai_classifiers, utilities
# ADDED IMPORT
from utils import constants

async def handle_direct_reply(bot_instance, message: discord.Message):
    """Handles a direct reply (via reply or mention) to one of the bot's messages."""
    
    replied_to_message = None
    if message.reference and message.reference.message_id:
        try:
            replied_to_message = await message.channel.fetch_message(message.reference.message_id)
        except:
            pass
    else:
        async for prior_message in message.channel.history(limit=10):
            if prior_message.author == bot_instance.user:
                replied_to_message = prior_message
                break
    
    if not replied_to_message:
        await handle_text_or_image_response(bot_instance, message, is_autonomous=False)
        return

    if len(message.content) > 3:
        impact_score = await ai_classifiers.analyze_sentiment_impact(
            bot_instance, message.author.display_name, message.content
        )
        if impact_score != 0:
            user_id = str(message.author.id)
            guild_id = str(message.guild.id) if message.guild else None
            
            new_score = await bot_instance.firestore_service.update_relationship_score(
                user_id, guild_id, impact_score
            )
            
            await update_relationship_status(bot_instance, user_id, guild_id, new_score)
            
            if impact_score > 0: 
                logging.info(f"📈 {message.author.display_name} gained {impact_score} pts via Reply. Total: {new_score:.2f}")
            else: 
                logging.info(f"📉 {message.author.display_name} lost {impact_score} pts via Reply. Total: {new_score:.2f}")
                
    user_name_to_use = await bot_instance.firestore_service.get_user_nickname(str(message.author.id)) or message.author.display_name
    
    reply_prompt = (
        f"{bot_instance.personality_instruction}\n\n"
        f"# --- CONVERSATION CONTEXT ---\n"
        f"You previously said: \"{replied_to_message.content}\"\n"
        f"The user '{user_name_to_use}' has now directly replied to you with: \"{message.content}\"\n\n"
        f"# --- YOUR TASK ---\n"
        f"Based on this direct reply, generate a short, in-character response. Your mood is '{bot_instance.current_mood}'."
    )

    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[reply_prompt],
            config=bot_instance.GEMINI_TEXT_CONFIG
        )
        
        # --- FIXED: Handle Empty Responses ---
        if response and response.text:
            cleaned_response = response.text.strip()
            if cleaned_response and cleaned_response.lower() != '[silence]':
                for chunk in bot_instance.split_message(cleaned_response):
                    await message.channel.send(chunk.lower())
        else:
            # Fallback if AI replies with nothing
            logging.warning(f"⚠️ Direct reply resulted in empty text. Candidate: {response.candidates[0] if response and response.candidates else 'None'}")
            await message.channel.send("huh? sorry i spaced out for a second.")
            
    except Exception:
        logging.error("Failed to handle direct reply.", exc_info=True)
        await message.channel.send("my brain just shorted out for a second, what were we talkin about?")

async def handle_text_or_image_response(bot_instance, message: discord.Message, is_autonomous: bool = False, summary: str = ""):
    """Core logic for generating a text response based on chat history."""
    async with bot_instance.channel_locks.setdefault(str(message.channel.id), asyncio.Lock()):
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else None
        user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
        rel_score = user_profile.get("relationship_score", 0)
        facts = user_profile.get("facts", {})
        facts_str = "\n".join([f"- {k}: {v}" for k, v in facts.items()]) if facts else "No specific facts remembered yet."

        # 🧠 TONE SELECTOR (Fixed Emoji)
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

        # --- HISTORY CONSTRUCTION ---
        history = [types.Content(role='user', parts=[types.Part(text=bot_instance.personality_instruction)])]

        if relevant_memories_text:
            history.append(types.Content(role='user', parts=[types.Part(text=f"## RECALLED MEMORIES FROM PAST CONVERSATIONS:\n(Use these only if relevant to the current topic)\n{relevant_memories_text}")]))

        history.append(types.Content(role='model', parts=[types.Part(text="aight, i get it. i'm vinny.")]))

        async for msg in message.channel.history(limit=bot_instance.MAX_CHAT_HISTORY_LENGTH, before=message):
            user_line = f"{msg.author.display_name} (ID: {msg.author.id}): {msg.content}"
            bot_line = f"{msg.author.display_name}: {msg.content}"
            history.append(types.Content(role="model" if msg.author == bot_instance.user else "user", parts=[types.Part(text=bot_line if msg.author == bot_instance.user else user_line)]))
        history.reverse()

        cleaned_content = re.sub(f'<@!?{bot_instance.user.id}>', '', message.content).strip()
        final_instruction_text = ""
        config = bot_instance.GEMINI_TEXT_CONFIG
        
        # 1. Handle Triage (Tools vs Chat)
        if "?" in message.content:
            question_type = await ai_classifiers.triage_question(bot_instance, cleaned_content)
            
            if question_type == "real_time_search":
                # Enable Search Tool
                config = types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    safety_settings=bot_instance.GEMINI_TEXT_CONFIG.safety_settings,
                    max_output_tokens=bot_instance.GEMINI_TEXT_CONFIG.max_output_tokens,
                    temperature=bot_instance.GEMINI_TEXT_CONFIG.temperature
                )
                final_instruction_text = (
                    "CRITICAL TASK: The user has asked a factual question requiring a search. You MUST use the provided Google Search tool to find a real-world, accurate answer. "
                    "FIRST, provide the direct, factual answer. AFTER providing the fact, you can add a short, in-character comment."
                )
            elif question_type == "general_knowledge":
                final_instruction_text = (
                    "CRITICAL TASK: The user has asked a factual question. Answer it accurately based on your internal knowledge. "
                    "FIRST, provide the direct, factual answer. AFTER providing the fact, you can add a short, in-character comment."
                )
            else: 
                final_instruction_text = (
                    f"{tone_instruction}\n\n"
                    f"## KNOWN USER FACTS:\n{facts_str}\n\n"
                    f"The user '{actual_display_name}' asked you a personal question. "
                    f"Answer them directly and honestly (in character). "
                    f"Do not summarize the chat. Just answer the question. Your mood is {bot_instance.current_mood}."
                )
        else:
            # Standard Chat logic
            if len(cleaned_content) < 4 and not message.attachments:
                final_instruction_text = (
                    f"{tone_instruction}\n\n"
                    f"## KNOWN USER FACTS:\n{facts_str}\n\n"
                    f"The user '{actual_display_name}' just said your name to get your attention. "
                    f"Chime in with an opinion on the recent chat topic, OR just acknowledge {actual_display_name}. "
                    f"3. **DO NOT SUMMARIZE.**"
                )
            else:
                final_instruction_text = (
                    f"{tone_instruction}\n\n"
                    f"## KNOWN USER FACTS:\n{facts_str}\n\n"
                    f"The user '{actual_display_name}' is talking directly to you. "
                    f"Respond ONLY to their last message: \"{cleaned_content}\". "
                    f"Just reply naturally to what they just said."
                )

        if is_autonomous:
            final_instruction_text = (
                f"{tone_instruction}\n\n"
                f"Your mood is {bot_instance.current_mood}. You are 'hanging out' in this chat server and just reading the messages above. "
                "Your task is to chime in naturally as if you were just another user.\n"
                "RULES:\n"
                "1. DO NOT summarize the conversation.\n"
                "2. Pick ONE specific thing a user said above and react to it directly.\n"
                "3. Be brief."
            )

        participants = set()
        async for msg in message.channel.history(limit=bot_instance.MAX_CHAT_HISTORY_LENGTH, before=message):
            if not msg.author.bot: participants.add(msg.author.display_name)
        participants.add(actual_display_name)
        participant_list = ", ".join(sorted(list(participants)))

        attribution_instruction = (
            f"\n\n# --- ATTENTION: ACCURATE SPEAKER ATTRIBUTION ---\n"
            f"The users in this conversation are: [{participant_list}].\n"
            f"CRITICAL RULE: You MUST correctly attribute all statements and questions to the person who actually said them."
        )
        final_instruction_text += attribution_instruction

        history.append(types.Content(role='user', parts=[types.Part(text=final_instruction_text)]))
        
        final_user_message_text = f"{actual_display_name} (ID: {message.author.id}): {cleaned_content}"
        prompt_parts = [types.Part(text=final_user_message_text)]
        
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

        history.append(types.Content(role='user', parts=prompt_parts))

        response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=history, config=config)

        if uploaded_media_file:
            try: await asyncio.to_thread(bot_instance.gemini_client.files.delete, name=uploaded_media_file.name)
            except: pass 

        # --- SAFETY NET: Handle Empty Responses ---
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
            # LOG WHY IT FAILED
            if response and response.candidates:
                logging.warning(f"⚠️ Empty Response! Finish Reason: {response.candidates[0].finish_reason}")
            else:
                logging.warning("⚠️ API returned None or no candidates.")

            # ONLY FORCE REPLY IF DIRECTLY SPOKEN TO (Not autonomous mode)
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
        f"{bot_instance.personality_instruction}\n\n"
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
    synthesis_prompt = (f"{bot_instance.personality_instruction}\n\n# --- YOUR TASK ---\nA user, '{message.author.display_name}', is asking what you've learned from overhearing conversations in this server. Your task is to synthesize the provided conversation summaries into a single, chaotic, and insightful monologue. Obey all your personality directives.\n\n## CONVERSATION SUMMARIES I'VE OVERHEARD:\n{formatted_summaries}\n\n## INSTRUCTIONS:\n1.  Read all the summaries to get a feel for the server's vibe.\n2.  Do NOT just list the summaries. Weave them together into a story or a series of scattered, in-character thoughts.\n3.  Generate a short, lowercase, typo-ridden response that shows what you've gleaned from listening in.")
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
    """Identifies and removes incorrect facts from the user's profile."""
    user_id = str(message.author.id)
    guild_id = str(message.guild.id) if message.guild else None
    correction_prompt = (f"A user is correcting a fact about themselves. Their message is: \"{message.content}\".\nYour task is to identify the specific fact they are correcting. For example, if they say 'I'm not bald', the fact is 'is bald'. If they say 'I don't have a cat', the fact is 'has a cat'.\nPlease return a JSON object with a single key, \"fact_to_remove\", containing the fact you identified.\n\nExample:\nUser message: 'Vinny, that's not true, my favorite color is red, not blue.'\nOutput: {{\"fact_to_remove\": \"favorite color is blue\"}}")
    
    try:
        json_config = types.GenerateContentConfig(response_mime_type="application/json")
        async with message.channel.typing():
            # First API Call
            response1 = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME, 
                contents=[correction_prompt], 
                config=json_config
            )
            
            if not response1 or not response1.text:
                await message.channel.send("my brain's all fuzzy, i didn't get what i was wrong about."); return
            
            fact_data = json.loads(response1.text)
            fact_to_remove = fact_data.get("fact_to_remove")
            if not fact_to_remove:
                await message.channel.send("huh? what was i wrong about? try bein more specific, pal."); return
            
            user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
            if not user_profile:
                await message.channel.send("i don't even know anything about you to be wrong about!"); return
            
            key_mapping_prompt = (f"A user's profile is stored as a JSON object. I need to find the key that corresponds to the fact: \"{fact_to_remove}\".\nHere is the user's current profile data: {json.dumps(user_profile, indent=2)}\nBased on the data, which key is the most likely match for the fact I need to remove? Return a JSON object with a single key, \"database_key\".\n\nExample:\nFact: 'is a painter'\nProfile: {{\"occupation\": \"a painter\"}}\nOutput: {{\"database_key\": \"occupation\"}}")
            
            # Second API Call
            response2 = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME, 
                contents=[key_mapping_prompt], 
                config=json_config
            )
            
            if not response2 or not response2.text:
                await message.channel.send("i thought i knew somethin' but i can't find it in my brain. weird."); return
            
            key_data = json.loads(response2.text)
            db_key = key_data.get("database_key")
            
            if not db_key or db_key not in user_profile:
                await message.channel.send("i thought i knew somethin' but i can't find it in my brain. weird."); return
            
            if await bot_instance.firestore_service.delete_user_profile_fact(user_id, guild_id, db_key):
                await message.channel.send(f"aight, my mistake. i'll forget that whole '{db_key.replace('_', ' ')}' thing. salute.")
            else:
                await message.channel.send("i tried to forget it, but the memory is stuck in there good. damn.")
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
                f"{bot_instance.personality_instruction}\n\n"
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

async def get_keywords_for_memory_search(bot_instance, text: str):
    """Extracts search keywords from a user message."""
    ignore_words = {"the", "and", "is", "it", "to", "in", "of", "that", "this", "for", "with", "you", "me", "vinny"}
    words = re.findall(r'\w+', text.lower())
    keywords = [w for w in words if w not in ignore_words and len(w) > 3]
    return keywords[:3]