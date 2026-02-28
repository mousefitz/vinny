import re
import json
import logging
import discord
import random
import io
import datetime
import base64
import fal_client
from PIL import Image
from google.genai import types
from utils import api_clients
from . import ai_classifiers

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

# --- PORTRAIT HANDLER (Simplified) ---
async def handle_portrait_request(bot_instance, message, target_users, details="", previous_prompt=None, input_image_bytes=None):
    """
    Collects user data and passes it to the main handler.
    """
    if not isinstance(target_users, list): target_users = [target_users]

    if message.guild:
        guild_id = str(message.guild.id)
        server_name = message.guild.name
    else:
        guild_id = None
        server_name = "Private Context"

    try:
        # GATHER DATA
        character_definitions = []
        appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'build', 'height', 'look', 'face', 'skin', 'beard', 'glasses', 'tattoo', 'piercing', 'scar', 'clothes', 'clothing', 'hat', 'mask', 'gender']
        PET_KEYWORDS = ['pet', 'dog', 'cat', 'bird', 'animal', 'horse', 'breed']
        is_pet_requested = any(word in details.lower() for word in PET_KEYWORDS)
        
        for i, user in enumerate(target_users, 1):
            appearance_facts = []
            pet_facts = []
            other_facts = []
            gender_fact = None

            if user.id == bot_instance.user.id:
                base_desc = "Robust middle-aged Italian-American man, long dark hair, messy beard, worn leather jacket"
                if input_image_bytes: character_definitions.append(base_desc)
                else: character_definitions.append(f"SUBJECT {i} (Vinny): [[VISUALS: {base_desc}]]")
                continue
            
            user_id = str(user.id)
            user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
            
            if user_profile:
                for key, value in user_profile.items():
                    clean_value = str(value).strip()
                    clean_key = key.replace('_', ' ').lower()
                    if re.search(r'\d{17,}', clean_value) or re.search(r'<@!?&?\d+>', clean_value): continue
                    
                    if 'gender' in clean_key: gender_fact = clean_value.title()
                    elif any(k in clean_key for k in PET_KEYWORDS): pet_facts.append(f"{clean_value}") 
                    elif any(k in clean_key for k in appearance_keywords): appearance_facts.append(f"{clean_key}: {clean_value}")
                    else: other_facts.append(clean_value)

            visuals_block = []
            if gender_fact and "unknown" not in gender_fact.lower(): visuals_block.append(gender_fact)
            if appearance_facts: visuals_block.extend(appearance_facts)
            else: visuals_block.append("person")
            
            visual_string = ", ".join(visuals_block)

            if input_image_bytes:
                # EDIT MODE: Raw visuals
                desc = f"a {visual_string}"
                if pet_facts and is_pet_requested: desc += f" accompanied by their {', '.join(pet_facts)}"
                character_definitions.append(desc)
            else:
                # GEN MODE: Tags
                char_str = f"SUBJECT {i} ({user.display_name}): [[VISUALS: {visual_string}]] "
                if pet_facts and is_pet_requested: char_str += f"[[MANDATORY PETS: {', '.join(pet_facts)}]] "
                elif pet_facts: other_facts.extend(pet_facts)
                if other_facts:
                    random.shuffle(other_facts)
                    char_str += f"[[TRIVIA: {', '.join(other_facts[:6])}]]"
                character_definitions.append(char_str)

        # PASS TO MAIN HANDLER
        final_prompt_text = ""
        if input_image_bytes:
            chars_desc = " and ".join(character_definitions)
            final_prompt_text = f"Add {chars_desc} to the image. {details}"
        else:
            user_request = f"USER REQUEST: {details}" if details else ""
            source_data = "\n".join(character_definitions)
            director_instruction = (
                "You are an expert AI Art Director.\n"
                f"**INPUT DATA:**\n{source_data}\n{user_request}\n\n"
                "**TASK:** Write a detailed image generation prompt.\n"
                "**OUTPUT:** Provide ONLY the final image prompt text."
            )
            style_response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[director_instruction]
            )
            final_prompt_text = style_response.text.strip() if style_response and style_response.text else source_data

        await handle_image_request(bot_instance, message, final_prompt_text, previous_prompt=previous_prompt, input_image_bytes=input_image_bytes)

    except Exception as e:
        logger.error(f"Portrait Gathering Failed: {e}", exc_info=True)
        await message.channel.send("i tried to gather the data, but i tripped.")

# --- MASTER IMAGE HANDLER ---
              
async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str, previous_prompt=None, input_image_bytes=None):
    """
    The Master Image Function: Handles Generation, Editing, and Vision.
    """
    async with message.channel.typing():
        # --- 0. CHECK FOR EDIT MODE ---
        is_edit_mode = (input_image_bytes is not None)
        
        # ==================================================================================
        # PATH A: EDIT MODE (The "Fix Everything" Path)
        # ==================================================================================
        if is_edit_mode:
            try:
                # 1. Prepare Image
                with Image.open(io.BytesIO(input_image_bytes)) as img:
                    if img.mode != 'RGB': img = img.convert('RGB')
                    # Resize to save cost/speed, keeps aspect ratio
                    if max(img.width, img.height) > 1024: img.thumbnail((1024, 1024))
                    buff = io.BytesIO()
                    img.save(buff, format="PNG")
                    img_str = base64.b64encode(buff.getvalue()).decode("utf-8")
                    image_url = f"data:image/png;base64,{img_str}"

                # 2. GET CONTEXT (Vision Fallback)
                # If we don't have a previous prompt (User Upload), we MUST look at it.
                if not previous_prompt:
                    await message.channel.send(random.choice(["looking at this...", "analyzing the image...", "studying the composition..."]))
                    vision_prompt = "Describe this image in detail. Focus on the subject, setting, and style."
                    
                    try:
                        # Convert bytes for Gemini Vision
                        vision_image = types.Part.from_bytes(data=input_image_bytes, mime_type="image/png")
                        vision_resp = await bot_instance.make_tracked_api_call(
                            model=bot_instance.MODEL_NAME,
                            contents=[vision_image, vision_prompt]
                        )
                        previous_prompt = vision_resp.text.strip() if vision_resp else "A photograph"
                    except:
                        previous_prompt = "A photograph"

                # 3. THE REWRITE (The "Fusion" Fix)
                # We do not append. We REWRITE the scene description.
                await message.channel.send(random.choice(["rewriting the reality...", "blending it in...", "remixing the scene..."]))
                
                rewriter_instruction = (
                    "You are an expert AI Prompt Engineer.\n"
                    f"**ORIGINAL IMAGE CONTEXT:** {previous_prompt}\n"
                    f"**USER MODIFICATION:** {image_prompt}\n\n"
                    "**TASK:** Write a SINGLE, cohesive paragraph that describes the NEW image.\n"
                    "**RULES:**\n"
                    "1. **INTEGRATE:** Do not say 'add a cat'. Say 'a cat sitting on the table'. Describe the RESULT.\n"
                    "2. **RETAIN:** Keep the style and lighting of the original context.\n"
                    "3. **PRIORITY:** The User Modification is mandatory.\n"
                    "**OUTPUT:** The raw prompt text only."
                )

                try:
                    rewrite_resp = await bot_instance.make_tracked_api_call(
                        model=bot_instance.MODEL_NAME,
                        contents=[rewriter_instruction]
                    )
                    enhanced_prompt = rewrite_resp.text.strip()
                except:
                    # Fallback if Gemini fails: Put the new thing FIRST (Priority hacking)
                    enhanced_prompt = f"{image_prompt}. {previous_prompt}"

                # --- NEW: MINOR SAFETY CHECK (EDIT PATH) ---
                is_safe = await ai_classifiers.is_prompt_safe_for_minors(bot_instance, enhanced_prompt)
                if not is_safe:
                    await message.channel.send("yeah, no. i ain't painting that. keep it clean when kids are involved, pal.")
                    return None
                # -------------------------------------------

                # 4. EXECUTE (Fal.ai Flash Edit)
                logger.info(f"🎨 Edit Prompt: '{enhanced_prompt[:100]}...'")
                handler = await fal_client.submit_async(
                    "fal-ai/flux-2/flash/edit", 
                    arguments={
                        "prompt": enhanced_prompt,
                        "image_urls": [image_url],
                        "strength": 0.95, # High strength to force the change
                        "guidance_scale": 3.5,
                        "num_inference_steps": 8,
                        "enable_safety_checker": False,
                        "num_images": 1
                    }
                )
                
                # 5. Process & Send
                result = await handler.get()
                if result and "images" in result and len(result["images"]) > 0:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(result["images"][0]["url"]) as resp:
                            if resp.status == 200:
                                image_obj = io.BytesIO(await resp.read())
                    
                    file = discord.File(image_obj, filename="vinny_edit.png")
                    embed = discord.Embed(title="🎨 Image Edit", color=discord.Color.dark_teal())
                    embed.set_image(url="attachment://vinny_edit.png")
                    # Save the NEW prompt for future edits
                    embed.set_footer(text=f"{enhanced_prompt[:1000]} | Edit by {message.author.display_name}")
                    
                    await message.channel.send(file=file, embed=embed)
                    
                    try:
                        today = datetime.datetime.now().strftime("%Y-%m-%d")
                        await bot_instance.firestore_service.update_usage_stats(today, {"images": 1, "cost": 0.01})
                    except: pass
                    return enhanced_prompt
                else:
                    await message.channel.send("i spilled the paint.")
                    return None

            except Exception as e:
                logger.error(f"Edit failed: {e}")
                await message.channel.send("my brain's fried. i can't edit right now.")
                return None

        # ==================================================================================
        # PATH B: GENERATION PATH (New Images)
        # ==================================================================================
        else:
            # (Standard Generation Logic)
            context_block = ""
            if previous_prompt:
                context_block = f"\n## HISTORY:\nPrevious: \"{previous_prompt}\". Ignore unless Edit keywords used.\n"

            prompt_rewriter_instruction = (
                "You are an AI Art Director. Refine the user's request into an image prompt.\n"
                f"{context_block}\n"
                "## Instructions:\n"
                "1. Include every specific object requested.\n"
                "2. Pick a unique style.\n"
                f"## Request:\n\"{image_prompt}\"\n"
                "## Output:\nJSON with 'core_subject' and 'enhanced_prompt'."
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

                # --- NEW: MINOR SAFETY CHECK (GENERATION PATH) ---
                is_safe = await ai_classifiers.is_prompt_safe_for_minors(bot_instance, enhanced_prompt)
                if not is_safe:
                    await message.channel.send("yeah, no. i ain't drawing that. keep it clean when kids are involved, pal.")
                    return None
                # -------------------------------------------------

                await message.channel.send(random.choice(["mixing the paints...", "loading the canvas..."]))

                image_obj, count = await api_clients.generate_image_with_genai(bot_instance.FAL_KEY, enhanced_prompt, model="fal-ai/flux-2/flash")

                if image_obj and count > 0:
                    try:
                        cost = api_clients.calculate_cost("fal-ai/flux-2/flash", "image", count=count)
                        today = datetime.datetime.now().strftime("%Y-%m-%d")
                        await bot_instance.firestore_service.update_usage_stats(today, {"images": count, "cost": cost})
                    except: pass
                    
                    file = discord.File(image_obj, filename="vinny_art.png")
                    embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                    embed.set_image(url="attachment://vinny_art.png")
                    embed.set_footer(text=f"{enhanced_prompt[:1000]} | Requested by {message.author.display_name}")
                    await message.channel.send(file=file, embed=embed)
                    return enhanced_prompt
                else:
                    await message.channel.send("i spilled the paint.")
                    return None
            except Exception as e:
                logger.error(f"Gen failed: {e}")
                await message.channel.send("my brain's fried.")
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
            f"# --- YOUR TASK ---\nA user, '{reply_message.author.display_name}', "
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
                contents=[types.Content(parts=prompt_parts)],
                config=bot_instance.GEMINI_TEXT_CONFIG
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
