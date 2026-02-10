import re
import json
import logging
import discord
import random
import io
import datetime
import base64
import fal_client
from PIL import Image  # <--- NEW IMPORT
from google.genai import types
from utils import api_clients

# Setup Logger
logger = logging.getLogger(__name__)

# --- NEW HELPER: SANITIZE IMAGES ---
def prepare_image_for_api(image_bytes):
    """
    Uses Pillow to convert any image (WebP, PNG, etc.) to a standard
    resized JPEG to ensure the Vision API accepts it.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # 1. Convert to RGB (Strip transparency/Alpha channel)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # 2. Resize if massive (Cap at 1024x1024 to save bandwidth/tokens)
            max_size = 1024
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size))
            
            # 3. Save to Bytes as JPEG
            output_buffer = io.BytesIO()
            img.save(output_buffer, format='JPEG', quality=85)
            output_buffer.seek(0)
            return output_buffer.read(), 'image/jpeg'
            
    except Exception as e:
        logger.warning(f"Pillow failed to process image: {e}. Using raw bytes.")
        # Fallback to original bytes if Pillow fails
        return image_bytes, 'image/png'

# --- PORTRAIT HANDLER (Now supports Edits!) ---
async def handle_portrait_request(bot_instance, message, target_users, details="", previous_prompt=None, input_image_bytes=None):
    """
    Generates an artistic depiction for users, or adds them to an existing image.
    """
    if not isinstance(target_users, list): target_users = [target_users]

    # 1. SETUP
    if message.guild:
        guild_id = str(message.guild.id)
        server_name = message.guild.name
    else:
        guild_id = None
        server_name = "Private Context"

    logger.info(f"Starting Portrait Request for {len(target_users)} users in {server_name}")

    try:
        # 2. GATHER DATA
        character_definitions = []
        appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'build', 'height', 'look', 'face', 'skin', 'beard', 'glasses', 'tattoo', 'piercing', 'scar', 'clothes', 'clothing', 'hat', 'mask', 'gender']
        PET_KEYWORDS = ['pet', 'dog', 'cat', 'bird', 'animal', 'horse', 'breed']
        is_pet_requested = any(word in details.lower() for word in PET_KEYWORDS)
        
        for i, user in enumerate(target_users, 1):
            appearance_facts = []
            pet_facts = []
            other_facts = []
            gender_fact = None

            # A. SELF-PORTRAIT OVERRIDE
            if user.id == bot_instance.user.id:
                char_def = (f"SUBJECT {i} (Vinny): [[VISUALS: Robust middle-aged Italian-American man, long dark hair, messy beard, worn pirate coat or leather jacket.]] [[TRIVIA: Chaos, Pizza, Eating Trash.]]")
                character_definitions.append(char_def)
                continue
            
            # B. USER LOOKUP
            user_id = str(user.id)
            user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
            
            if user_profile:
                for key, value in user_profile.items():
                    clean_value = str(value).strip()
                    clean_key = key.replace('_', ' ').lower()
                    if re.search(r'\d{17,}', clean_value) or re.search(r'<@!?&?\d+>', clean_value): continue
                    
                    if 'gender' in clean_key: gender_fact = clean_value.title()
                    elif any(k in clean_key for k in PET_KEYWORDS): pet_facts.append(f"{clean_key}: {clean_value}")
                    elif any(k in clean_key for k in appearance_keywords): appearance_facts.append(clean_value)
                    else: other_facts.append(clean_value)

            # C. SUBJECT CONSTRUCTION
            char_str = f"SUBJECT {i} ({user.display_name}): "
            visuals_block = []
            if gender_fact and "unknown" not in gender_fact.lower(): visuals_block.append(f"Gender: {gender_fact}")
            
            if appearance_facts: visuals_block.extend(appearance_facts)
            else: visuals_block.append("Visuals: Undefined (Invent a look)")
            
            char_str += f"[[VISUALS: {', '.join(visuals_block)}]] "

            if pet_facts and is_pet_requested: char_str += f"[[MANDATORY PETS: {', '.join(pet_facts)}]] "
            elif pet_facts: other_facts.extend(pet_facts)

            if other_facts:
                random.shuffle(other_facts)
                char_str += f"[[TRIVIA: {', '.join(other_facts[:6])}]]"
            else:
                char_str += "[[TRIVIA: Unknown]]"

            character_definitions.append(char_str)

        # 3. USER DETAILS
        user_request = f"USER REQUEST: {details}" if details else ""

        # 4. CREATIVE DIRECTOR STEP
        source_data = "\n".join(character_definitions)

        director_instruction = (
            "You are an expert AI Art Director.\n"
            f"**INPUT DATA:**\n{source_data}\n{user_request}\n\n"
            "**YOUR TASK:** Write a detailed image generation prompt.\n"
            "1. **VISUAL ACCURACY:** Describe characters exactly as defined in [[VISUALS]].\n"
            "2. **ACTION:** Make them engage with the scene based on [[TRIVIA]] or the USER REQUEST.\n"
            "3. **COMPOSITION:** Use dynamic angles. No boring passport photos.\n"
            "4. **ART STYLE:** Choose a unique art style.\n\n"
            "**OUTPUT:** Provide ONLY the final image prompt text."
        )

        style_response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[director_instruction]
        )

        final_prompt_text = style_response.text.strip() if style_response and style_response.text else source_data
        
        logger.info(f"GENERATED DETAILED PROMPT: {final_prompt_text}")

        # 5. EXECUTE (Pass through Edit params)
        await handle_image_request(
            bot_instance, 
            message, 
            final_prompt_text, 
            previous_prompt=previous_prompt,
            input_image_bytes=input_image_bytes
        )

    except Exception as e:
        logger.error(f"PAINT REQUEST FAILED in {server_name}: {e}", exc_info=True)
        await message.channel.send("i tried to paint ya, but i tripped.")
              
async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str, previous_prompt=None, input_image_bytes=None):
    """
    Generates or Edits an image using Gemini/Fal.ai.
    """
    async with message.channel.typing():
        # --- 0. SETUP EDIT MODE ---
        is_edit_mode = (input_image_bytes is not None)
        image_url = None
        
        if is_edit_mode:
            try:
                # Prepare image for API (Resize to 1MP max)
                with Image.open(io.BytesIO(input_image_bytes)) as img:
                    if img.mode != 'RGB': img = img.convert('RGB')
                    if max(img.width, img.height) > 1024: img.thumbnail((1024, 1024))
                    buff = io.BytesIO()
                    img.save(buff, format="PNG")
                    img_str = base64.b64encode(buff.getvalue()).decode("utf-8")
                    image_url = f"data:image/png;base64,{img_str}"
                
                # Merge prompts for context
                if previous_prompt:
                    enhanced_prompt = f"{previous_prompt}. {image_prompt}"
                else:
                    enhanced_prompt = image_prompt
                
                core_subject = "Image Edit"
                
            except Exception as e:
                logging.error(f"Edit Prep Failed: {e}")
                await message.channel.send("i can't see the picture clearly. it's all garbled.")
                return None

        # --- 1. REWRITER (Only run if NOT editing) ---
        elif not is_edit_mode:
            context_block = ""
            if previous_prompt:
                context_block = (
                    f"\n## HISTORY:\n"
                    f"Previous: \"{previous_prompt}\".\n"
                    f"Ignore unless User Request implies an edit.\n"
                )

            prompt_rewriter_instruction = (
                "You are an AI Art Director. Refine the user's request into an image prompt.\n\n"
                f"{context_block}\n"
                "## CRITICAL INSTRUCTIONS:\n"
                "1. **STRICT OBEDIENCE:** You MUST include every specific action/object the user requested.\n"
                "2. **STYLE:** Pick a unique style unless the user requested one.\n"
                f"## User Request:\n\"{image_prompt}\"\n\n"
                "## Your Output:\n"
                "Provide a single JSON object with 3 keys:\n"
                "- \"core_subject\" (Short title)\n"
                "- \"enhanced_prompt\" (The detailed image generation prompt)\n"
            )
            
            try:
                safety_settings_off = [types.SafetySetting(category=c, threshold="OFF") for c in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]]
                
                response = await bot_instance.make_tracked_api_call(
                    model=bot_instance.MODEL_NAME,
                    contents=[prompt_rewriter_instruction],
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.7, safety_settings=safety_settings_off)
                )
                
                if response and response.text:
                    data = json.loads(response.text)
                    enhanced_prompt = data.get("enhanced_prompt", image_prompt)
                    core_subject = data.get("core_subject", "Artistic Chaos")
                else:
                    enhanced_prompt = image_prompt
                    core_subject = "Artistic Chaos"

            except Exception:
                enhanced_prompt = image_prompt
                core_subject = "Artistic Chaos"

        # --- 2. ANNOUNCE ---
        thinking_messages = [
            "aight, gimme a sec. i gotta find my brushes.",
            "oh i got a vision. hold on.",
            "mixing the paints... don't rush me.",
            "lemme see what i can do. stand back.",
            "hold on, let me finish this slice of pizza first.",
            "patience. art takes time, unlike your attention span.",
            "i'm on it. this is gonna be messy.",
            "loading the canvas... aka grabbing a napkin.",
            "got it. preparing the masterpiece.",
            "mixing the paints... let's see what happens.",
            "wiping the canvas... startin' fresh.",
            "i got a wild idea for this one.",
            "loading the canvas...",
        ]
        await message.channel.send(random.choice(thinking_messages))

        # --- 3. EXECUTE (Flash Gen or Flash Edit) ---
        try:
            if is_edit_mode:
                logging.info(f"🎨 Fal.ai FLASH EDIT: '{enhanced_prompt}'")
                handler = await fal_client.submit_async(
                    "fal-ai/flux-2/flash/edit", 
                    arguments={
                        "prompt": enhanced_prompt,
                        "image_urls": [image_url],
                        "strength": 0.85,
                        "guidance_scale": 3.5,
                        "num_inference_steps": 8,
                        "enable_safety_checker": False,
                        "num_images": 1
                    }
                )
            else:
                image_obj, count = await api_clients.generate_image_with_genai(bot_instance.FAL_KEY, enhanced_prompt, model="fal-ai/flux-2/flash")
                # Normalize result for processing below
                result = None 

            # --- 4. PROCESS EDIT RESULT ---
            if is_edit_mode:
                result = await handler.get()
                if result and "images" in result and len(result["images"]) > 0:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(result["images"][0]["url"]) as resp:
                            if resp.status == 200:
                                image_obj = io.BytesIO(await resp.read())
                    count = 1
                    core_subject = "Image Edit"
                else:
                    image_obj = None

            # --- 5. SEND ---
            if image_obj:
                try:
                    cost = 0.01 if is_edit_mode else 0.005 
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    await bot_instance.firestore_service.update_usage_stats(today, {"images": 1, "cost": cost})
                except: pass
                
                file = discord.File(image_obj, filename="vinny_art.png")
                embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                embed.set_image(url="attachment://vinny_art.png")
                
                footer = f"{enhanced_prompt[:1000]} | Requested by {message.author.display_name}"
                if is_edit_mode: footer += " (Edit)"
                embed.set_footer(text=footer)
                
                await message.channel.send(file=file, embed=embed)
                return enhanced_prompt
            else:
                await message.channel.send("i spilled the paint. something went wrong.")
                return None
        
        except Exception as e:
            logging.error(f"Image Task Failed: {e}")
            await message.channel.send("my brain's fried. i can't paint right now.")
            return None

# --- 3. IMAGE REPLIES (Comments) ---

async def handle_image_reply(bot_instance, reply_message: discord.Message, original_message: discord.Message):
    """Responds to a user's comment on an existing image."""
    try:
        image_url = None
        # Default, but will be overwritten by Pillow helper
        mime_type = 'image/png' 

        if original_message.embeds and original_message.embeds[0].image:
            image_url = original_message.embeds[0].image.url
        elif original_message.attachments and "image" in original_message.attachments[0].content_type:
            image_attachment = original_message.attachments[0]
            image_url = image_attachment.url

        if not image_url:
            await reply_message.channel.send("i see the reply but somethin's wrong with the original picture, pal.")
            return

        async with bot_instance.http_session.get(image_url) as resp:
            if resp.status != 200:
                await reply_message.channel.send("couldn't grab the picture, the link's all busted.")
                return
            raw_bytes = await resp.read()

        # --- USE PILLOW TO CLEANUP IMAGE ---
        clean_bytes, clean_mime = prepare_image_for_api(raw_bytes)
        # -----------------------------------

        user_comment = re.sub(f'<@!?{bot_instance.user.id}>', '', reply_message.content).strip()
        prompt_text = (
            f"{bot_instance.personality_instruction}\n\n# --- YOUR TASK ---\nA user, '{reply_message.author.display_name}', "
            f"just replied to the attached image with the comment: \"{user_comment}\".\nYour task is to look "
            f"at the image and respond to their comment in your unique, chaotic, and flirty voice."
        )
        
        prompt_parts = [
            types.Part(text=prompt_text),
            types.Part(inline_data=types.Blob(mime_type=clean_mime, data=clean_bytes))
        ]

        async with reply_message.channel.typing():
            response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[types.Content(parts=prompt_parts)]
            )
            
            if response and response.text:
                for chunk in bot_instance.split_message(response.text): 
                    await reply_message.channel.send(chunk.lower())
            else:
                # Log refusal details
                finish_reason = "Unknown"
                if response and response.candidates:
                    finish_reason = response.candidates[0].finish_reason
                
                logging.warning(f"Image Reply Blocked/Empty. Finish Reason: {finish_reason}")
                await reply_message.channel.send("i see it, but the safety filters are gagging me. can't talk about it.")

    except Exception:
        logging.error("Failed to handle an image reply.", exc_info=True)
        await reply_message.channel.send("my eyes are all blurry, couldn't make out the picture, pal.")
