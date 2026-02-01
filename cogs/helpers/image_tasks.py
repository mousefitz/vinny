import re
import json
import logging
import discord
import random
import os
import datetime
from google.genai import types
from utils import api_clients

from discord.ext import commands

# Setup Logger
logger = logging.getLogger(__name__)

async def handle_portrait_request(bot_instance, message, target_users, details=""):
    """
    Generates an artistic depiction for ONE or MULTIPLE users.
    Includes:
    1. Strict Server Isolation (No cross-server data leaks).
    2. Data Sanitation (Removes User IDs/Mentions from prompts).
    3. Robust Error Logging.
    """
    if not isinstance(target_users, list):
        target_users = [target_users]

    # 1. SETUP & CONTEXT (Strict Isolation)
    if message.guild:
        guild_id = str(message.guild.id)
        server_name = message.guild.name
    else:
        guild_id = None
        server_name = "Private Context"

    # Log start of request
    logger.info(f"Starting Paint Request for {len(target_users)} users in {server_name} ({guild_id})")

    try:
        # 2. BUILD PROMPT
        prompt_parts = [
            f"A high-quality artistic depiction of {len(target_users)} people.",
            "The composition should be natural, dynamic, or environmental.",
            f"Setting: A scene consistent with the vibe of {server_name}." 
        ]
        
        appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'build', 'height', 'look', 'face', 'skin', 'beard', 'glasses']

        for i, user in enumerate(target_users, 1):
            # CLEAR VARIABLES (Prevent Loop Bleed)
            appearance_facts = []
            other_facts = []
            gender_fact = None
            desc = ""

            # --- A. SELF-PORTRAIT OVERRIDE (Vinny) ---
            if user.id == bot_instance.user.id:
                desc = (f"**Subject {i} (Vinny):** "
                        "Appearance: Robust middle-aged Italian-American man, long dark hair, messy beard, wearing a worn pirate coat or leather jacket. "
                        "Activity/Vibe: Chaotic, confident, holding a tool or drink.")
                prompt_parts.append(desc)
                continue
            
            # --- B. USER LOOKUP (Scoped to Guild) ---
            user_id = str(user.id)
            user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
            
            if user_profile:
                for key, value in user_profile.items():
                    clean_value = str(value).strip()
                    clean_key = key.replace('_', ' ')

                    # --- SANITATION FILTER ---
                    # Reject if the fact contains a long ID string (17+ digits) or mention syntax
                    # This fixes the "strings of userids" issue.
                    if re.search(r'\d{17,}', clean_value) or re.search(r'<@!?&?\d+>', clean_value):
                        continue
                    # -------------------------
                    
                    if 'gender' in clean_key:
                        gender_fact = clean_value.title()
                    elif any(keyword in clean_key for keyword in appearance_keywords): 
                        appearance_facts.append(clean_value)
                    else: 
                        other_facts.append(clean_value)

            # --- C. SUBJECT DESCRIPTION ---
            desc = f"**Subject {i} ({user.display_name}):** "
            
            # Gender
            if gender_fact and "unknown" not in gender_fact.lower():
                desc += f"Gender: {gender_fact}. "
            
            # Visuals
            if appearance_facts:
                desc += f"Visuals: {', '.join(appearance_facts)}. "
            else:
                desc += "Visuals: Undefined (be creative). "
            
            # Activity / Vibe (From Interests)
            if other_facts:
                random.shuffle(other_facts)
                selected_facts = other_facts[:3]
                desc += f"Action/Vibe based on: {', '.join(selected_facts)}."
                
            prompt_parts.append(desc)

        # 3. USER DETAILS
        if details:
            prompt_parts.append(f"**Specific Instruction:** {details}")

        # 4. FINALIZE PROMPT
        final_prompt_text = " ".join(prompt_parts)
        
        # LOG THE PROMPT (For Debugging)
        logger.info(f"GENERATING IMAGE PROMPT: {final_prompt_text}")

        # 5. EXECUTE (Stateless)
        # previous_prompt=None prevents LLM context leaks
        await handle_image_request(
            bot_instance, 
            message, 
            final_prompt_text, 
            previous_prompt=None 
        )

    except Exception as e:
        # ROBUST ERROR LOGGING
        error_msg = f"PAINT REQUEST FAILED in {server_name}: {e}"
        logger.error(error_msg, exc_info=True) # exc_info=True prints the full traceback
        
       
# --- 2. SELF PORTRAITS (Wrapper) ---

async def handle_paint_me_request(bot_instance, message: discord.Message):
    await handle_portrait_request(bot_instance, message, [message.author], details="")


# --- 3. GENERIC IMAGE REQUESTS (Rewriter & Generator) ---

async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str, previous_prompt=None):
    """
    Generates an image using Gemini to rewrite the prompt.
    """
    async with message.channel.typing():
        # 1. Rewriter Instruction
        context_block = ""
        if previous_prompt:
            context_block = (
                f"\n## HISTORY (OLD GARBAGE - IGNORE MOST OF THE TIME):\n"
                f"The previous image was: \"{previous_prompt}\".\n"
                f"**STRICT RULE:** Only use this history if the User Request contains EDIT keywords.\n"
                f"**OTHERWISE: THROW THIS HISTORY AWAY AND START FRESH.**\n"
            )

        prompt_rewriter_instruction = (
            "You are an AI Art Director. Refine the user's request into an image prompt.\n\n"
            f"{context_block}\n"
            "## REFERENCE GUIDE:\n"
            f"1. **THE USER:** '{message.author.display_name}'.\n"
            f"2. **VINNY (YOU):** Robust middle-aged Italian-American man, long dark hair, beard, pirate coat.\n\n"
            "## CRITICAL INSTRUCTIONS:\n"
            "1. **NEW SUBJECT = NEW PROMPT:** If the user asks for 'a dog' and the history was 'a cat', DELETE THE CAT. Output ONLY 'a dog'.\n"
            "2. **CONTRADICTION:** If the request contradicts the history, the REQUEST WINS.\n"
            "3. **GENDER PRIORITY:** If the prompt specifies a gender (e.g. 'Gender: Male', 'draw a man'), YOU MUST OBEY IT. Do not let hair length or clothes override the gender.\n"
            "4. **STYLE:** Preserve the user's requested style (anime, oil, photo).\n"
            "5. **KEEP THE CHAOS:** If the user asks for something weird, gross, or chaotic, INCLUDE IT.\n\n"
            f"## User Request:\n\"{image_prompt}\"\n\n"
            "## Your Output:\n"
            "Provide a single JSON object with keys: \"core_subject\" (2-5 words) and \"enhanced_prompt\" (full description)."
        )

        try:
            # 2. Generate Enhanced Prompt
            # Safety Settings OFF for the REWRITER so it doesn't block the prompt text
            safety_settings_off = [
                types.SafetySetting(category=cat, threshold="OFF")
                for cat in [
                    types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                ]
            ]
            
            response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[prompt_rewriter_instruction],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.7, # Higher temp for more creativity
                    safety_settings=safety_settings_off
                )
            )
            
            if not response or not response.text:
                await message.channel.send("my muse is on vacation. try again.")
                return None

            data = json.loads(response.text)
            enhanced_prompt = data.get("enhanced_prompt", image_prompt)
            core_subject = data.get("core_subject", "something weird")
            
            # 3. Announce
            thinking_messages = [
                "aight, gimme a sec. i gotta find my brushes.",
                "oh i got a vision. hold on.",
                "mixing the paints... don't rush me.",
                "lemme see what i can do. stand back.",
                "hold on, let me finish this slice of pizza first.",
                "patience. art takes time, unlike your attention span.",
                "i'm on it. this is gonna be messy.",
                "loading the canvas... aka grabbing a napkin.",
                "got it. preparing the masterpiece."
                "mixing the paints... let's see what happens.",
                "wiping the canvas... startin' fresh.",
                "i got a wild idea for this one.",
                "loading the canvas...",
                "patience, art takes time."
            ]
            
            await message.channel.send(random.choice(thinking_messages))

            # 4. Generate the Image
            image_obj, count = await api_clients.generate_image_with_genai(
                bot_instance.gemini_client,
                enhanced_prompt,
                model="imagen-4.0-fast-generate-001" 
            )

            if image_obj and count > 0:
                try:
                    cost = api_clients.calculate_cost("imagen-4.0-fast-generate-001", "image", count=count)
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    await bot_instance.firestore_service.update_usage_stats(today, {"images": count, "cost": cost})
                except Exception: pass
                
                file = discord.File(image_obj, filename="vinny_art.png")
                embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                embed.set_image(url="attachment://vinny_art.png")
                
                clean_prompt = enhanced_prompt[:1000].replace("\n", " ")
                embed.set_footer(text=f"{clean_prompt} | Requested by {message.author.display_name}")
                
                await message.channel.send(file=file, embed=embed)
                return enhanced_prompt
            
            else:
                await message.channel.send("i spilled the paint. something went wrong.")
                return None

        except Exception as e:
            logging.error(f"Image generation failed: {e}")
            await message.channel.send("my brain's fried. i can't paint right now.")
            return None


# --- 4. IMAGE REPLIES (Comments) ---

async def handle_image_reply(bot_instance, reply_message: discord.Message, original_message: discord.Message):
    """Responds to a user's comment on an existing image."""
    try:
        image_url = None
        mime_type = 'image/png'

        if original_message.embeds and original_message.embeds[0].image:
            image_url = original_message.embeds[0].image.url
        elif original_message.attachments and "image" in original_message.attachments[0].content_type:
            image_attachment = original_message.attachments[0]
            image_url = image_attachment.url
            mime_type = image_attachment.content_type

        if not image_url:
            await reply_message.channel.send("i see the reply but somethin's wrong with the original picture, pal.")
            return

        async with bot_instance.http_session.get(image_url) as resp:
            if resp.status != 200:
                await reply_message.channel.send("couldn't grab the picture, the link's all busted.")
                return
            image_bytes = await resp.read()

        user_comment = re.sub(f'<@!?{bot_instance.user.id}>', '', reply_message.content).strip()
        prompt_text = (
            f"{bot_instance.personality_instruction}\n\n# --- YOUR TASK ---\nA user, '{reply_message.author.display_name}', "
            f"just replied to the attached image with the comment: \"{user_comment}\".\nYour task is to look "
            f"at the image and respond to their comment in your unique, chaotic, and flirty voice."
        )
        
        prompt_parts = [
            types.Part(text=prompt_text),
            types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes))
        ]

        async with reply_message.channel.typing():
            response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[types.Content(parts=prompt_parts)]
            )
            
            if response and response.text:
                for chunk in bot_instance.split_message(response.text): 
                    await reply_message.channel.send(chunk.lower())

    except Exception:
        logging.error("Failed to handle an image reply.", exc_info=True)
        await reply_message.channel.send("my eyes are all blurry, couldn't make out the picture, pal.")
