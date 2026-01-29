import re
import json
import logging
import discord
import random
import os
import datetime
from google.genai import types
from utils import api_clients

# --- 1. PORTRAIT REQUESTS (Unified "Old Style" Logic) ---

async def handle_portrait_request(bot_instance, message, target_user, details=""):
    """
    Generates a portrait using the CLASSIC Vinny logic (Profile-based).
    Works for both Self-Portraits and Other Users.
    """
    user_id = str(target_user.id)
    guild_id = str(message.guild.id) if message.guild else None
    
    # 1. Fetch Profile
    user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
    
    # 2. Filter Facts
    appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'gender', 'build', 'height', 'look', 'face', 'skin', 'beard', 'glasses']
    appearance_facts = {}
    other_facts = {}
    
    if user_profile:
        for key, value in user_profile.items():
            if any(keyword in key.replace('_', ' ') for keyword in appearance_keywords): 
                appearance_facts[key] = value
            else: 
                other_facts[key] = value

    # 3. Construct the "Masterpiece" Prompt
    prompt_text = f"An artistic, masterpiece oil painting of a person named {target_user.display_name}."
    
    # A. Add Physical Description
    if appearance_facts:
        appearance_desc = ", ".join([f"{value}" for key, value in appearance_facts.items()])
        prompt_text += f" They are described as having {appearance_desc}."
    else:
        prompt_text += " Their appearance is unknown, so be creative."

    # B. Add the Specific User Request
    if details:
        prompt_text += f" They are depicted {details}."

    # C. Add Vibe/Theme
    if other_facts:
        items = list(other_facts.items())
        random.shuffle(items)
        selected_facts = items[:5]
        other_desc = ", ".join([f"{key.replace('_', ' ')} is {value}" for key, value in selected_facts])
        prompt_text += f" The painting's theme and background should be inspired by these traits: {other_desc}."

    # 4. Pass to the Image Generator
    await handle_image_request(bot_instance, message, prompt_text, previous_prompt=None)


# --- 2. SELF PORTRAITS (Wrapper) ---

async def handle_paint_me_request(bot_instance, message: discord.Message):
    """Wrapper that redirects 'paint me' to the unified portrait logic."""
    await handle_portrait_request(bot_instance, message, message.author, details="")


# --- 3. GENERIC IMAGE REQUESTS (Rewriter & Generator) ---

async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str, previous_prompt=None):
    """
    Generates an image using Gemini to rewrite the prompt and Imagen 4 to paint it.
    """
    async with message.channel.typing():
        # 1. Rewriter Instruction
        context_instruction = ""
        if previous_prompt:
            context_instruction = (
                f"\n## CONTEXT (PREVIOUS PAINTING):\n"
                f"The last thing you painted in this channel was: \"{previous_prompt}\".\n"
                f"**CRITICAL DECISION - DO NOT MIX SUBJECTS:**\n"
                f"1. **DEFAULT TO IGNORE:** Assume the User Request is a NEW idea unless they explicitly ask to edit (e.g. 'change X', 'add Y').\n"
                f"2. **CONFLICT RESOLUTION:** If the Previous Subject was 'A Cat' and New Request is 'A Dog', draw ONLY A DOG.\n"
            )

        prompt_rewriter_instruction = (
            "You are an AI Art Director. Your goal is to refine user requests into detailed image generation prompts.\n\n"
            "## CRITICAL MEMORY PROTOCOL:\n"
            f"{context_instruction}\n"
            "## REFERENCE GUIDE:\n"
            f"1. **THE USER:** '{message.author.display_name}'. If they say 'me', use a generic person suitable for them (unless the prompt already describes them).\n"
            f"2. **VINNY (YOU):** Robust middle-aged Italian-American man, long dark hair, beard, pirate coat.\n\n"
            "## CONTRADICTION OVERRIDE PROTOCOL (IMPORTANT):\n"
            "If the User Request contradicts the Context (or the Input Description), the **REQUEST WINS**.\n"
            "- **Example:** Input says 'Person with blonde hair', Request says 'Make them bald'.\n"
            "- **Bad Output:** 'A person with blonde hair who is bald.' (Contradiction)\n"
            "- **Correct Output:** 'A bald person.' (DELETE the blonde hair).\n\n"
            "## VISUAL STYLE RULES:\n"
            "1. **PRESERVE INTENT:** If the user specifies a style (e.g. 'anime', 'oil', 'sketch'), LOCK IT IN.\n"
            "2. **ADAPTIVE ENHANCEMENT:** Do NOT default to 'cinematic' or '4k'. Use boosters that FIT the style:\n"
            "   - If 'Anime' -> 'high quality animation, vibrant, studio ghibli style'.\n"
            "   - If 'Photo' -> '4k, realistic texture, cinematic lighting'.\n"
            "   - If 'Oil Painting' -> 'detailed brushstrokes, texture, masterpiece'.\n"
            "3. **NO REPETITION:** Do not use the same lighting or color palette twice in a row.\n\n"
            f"## User Request:\n\"{image_prompt}\"\n\n"
            "## Your Output:\n"
            "Provide a single JSON object with keys: \"core_subject\" (2-5 words) and \"enhanced_prompt\" (full description)."
        )

        try:
            # 2. Generate Enhanced Prompt
            response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[prompt_rewriter_instruction],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=1.0 
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
                "i got a wild idea for this one.",
                "loading the canvas...",
                "patience, art takes time.",
                "preparing the masterpiece."
            ]
            await message.channel.send(random.choice(thinking_messages))

            # 4. Generate the Image
            image_obj, count = await api_clients.generate_image_with_genai(
                bot_instance.gemini_client,
                enhanced_prompt,
                model="imagen-4.0-fast-generate-001" 
            )

            if image_obj and count > 0:
                # --- TRACKING ---
                try:
                    cost = api_clients.calculate_cost("imagen-4.0-fast-generate-001", "image", count=count)
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    await bot_instance.firestore_service.update_usage_stats(today, {"images": count, "cost": cost})
                except Exception as e:
                    logging.error(f"Failed to track image cost: {e}")
                
                # --- SENDING ---
                file = discord.File(image_obj, filename="vinny_art.png")
                embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                embed.set_image(url="attachment://vinny_art.png")
                
                # Clean prompt for footer
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
