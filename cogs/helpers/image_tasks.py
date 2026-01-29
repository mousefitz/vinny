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
    
    # 2. Filter Facts (The "Old Way" you liked)
    appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'gender', 'build', 'height', 'look', 'face', 'skin', 'beard', 'glasses']
    appearance_facts = {}
    other_facts = {}
    
    if user_profile:
        for key, value in user_profile.items():
            # Check if key contains any visual keyword
            if any(keyword in key.replace('_', ' ') for keyword in appearance_keywords): 
                appearance_facts[key] = value
            else: 
                # Keep other facts for "Vibe/Theme"
                other_facts[key] = value

    # 3. Construct the "Masterpiece" Prompt
    # We use the exact phrasing from your old code because it produced good results.
    prompt_text = f"An artistic, masterpiece oil painting of a person named {target_user.display_name}."
    
    # A. Add Physical Description
    if appearance_facts:
        appearance_desc = ", ".join([f"{value}" for key, value in appearance_facts.items()])
        prompt_text += f" They are described as having {appearance_desc}."
    else:
        prompt_text += " Their appearance is unknown, so be creative."

    # B. Add the Specific User Request (e.g. "wearing a track suit")
    if details:
        prompt_text += f" They are depicted {details}."

    # C. Add Vibe/Theme from non-visual facts
    if other_facts:
        # Limit to 5 random facts to prevent prompt bloating
        items = list(other_facts.items())
        random.shuffle(items)
        selected_facts = items[:5]
        other_desc = ", ".join([f"{key.replace('_', ' ')} is {value}" for key, value in selected_facts])
        prompt_text += f" The painting's theme and background should be inspired by these traits: {other_desc}."

    # 4. Pass to the Image Generator
    # We call handle_image_request which handles the "Vinny Thinking" messages and API call.
    # Note: We pass 'prompt_text' as the prompt.
    await handle_image_request(bot_instance, message, prompt_text)


# --- 2. SELF PORTRAITS (Wrapper) ---

async def handle_paint_me_request(bot_instance, message: discord.Message):
    """Wrapper that redirects 'paint me' to the unified portrait logic."""
    await handle_portrait_request(bot_instance, message, message.author, details="")

# --- 3. GENERIC IMAGE REQUESTS (New Style with Gemini + Imagen 4) ---
async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str, previous_prompt=None):
    """
    Generates an image using Gemini to rewrite the prompt and Imagen 4 to paint it.
    Returns the FINAL enhanced prompt used, so it can be saved to history.
    """
    async with message.channel.typing():
        # 1. Rewriter Instruction (Now with Context Awareness)
        context_instruction = ""
        if previous_prompt:
            context_instruction = (
                f"\n## CONTEXT (PREVIOUS PAINTING):\n"
                f"The last thing you painted for this user was: \"{previous_prompt}\".\n"
                f"**CRITICAL DECISION:**\n"
                f"- IF the User Request implies an edit (e.g., 'add a hat', 'make it night', 'remove the dog'), MERGE the previous prompt with the new request to create a complete updated scene.\n"
                f"- IF the User Request is a completely new idea (e.g., 'draw a car'), IGNORE the previous painting.\n"
            )

        prompt_rewriter_instruction = (
            "You are an AI Art Director. Your goal is to refine user requests into detailed image generation prompts.\n\n"
            "## CRITICAL MEMORY PROTOCOL:\n"
            f"{context_instruction}\n"
            "## REFERENCE GUIDE (WHO IS WHO):\n"
            f"1. **THE USER:** The requester's name is **'{message.author.display_name}'**.\n"
            f"   - If they say 'me', 'myself', or 'I', they mean **'{message.author.display_name}'** (NOT YOU).\n"
            f"   - If they say 'us', include both '{message.author.display_name}' and Vinny.\n"
            "2. **VINNY (YOU):** If they say 'you', 'yourself', or 'Vinny', they mean YOU.\n"
            "   - Vinny's Look: Robust middle-aged Italian-American man, long wild dark hair, full beard, dark blue pirate coat.\n\n"
            "## RULES FOR MODIFICATIONS:\n"
            "1. **LOCK THE SUBJECT:** If editing (e.g. 'add a hat'), KEEP the previous subject. Do not change a Whale into a Human.\n"
            "2. **INTERPRET 'ME' CORRECTLY:** If the user asks for 'me', describe a generic person suitable for '{message.author.display_name}' (unless you know their specific look), but DO NOT draw Vinny.\n"
            "3. **MERGE DETAILS:** Combine context with new requests.\n\n"
            "## VISUAL STYLE RULES:\n"
            "1. **MATCH THE VIBE:** Happy = Bright/Soft. Scary = Dark/Gritty.\n"
            "2. **ENHANCE:** Add 'cinematic lighting', '4k', 'detailed texture'.\n\n"
            f"## User Request:\n\"{image_prompt}\"\n\n"
            "## Your Output:\n"
            "Provide a single JSON object with keys: \"core_subject\" (2-5 words) and \"enhanced_prompt\" (full description)."
        )

        try:
            # 2. Generate Enhanced Prompt
            response = await bot_instance.make_tracked_api_call(
                model=bot_instance.MODEL_NAME,
                contents=[prompt_rewriter_instruction],
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            
            if not response or not response.text:
                await message.channel.send("my muse is on vacation. try again.")
                return None

            data = json.loads(response.text)
            enhanced_prompt = data.get("enhanced_prompt", image_prompt)
            core_subject = data.get("core_subject", "something weird")
            
            # 3. Announce the "Thinking" Phase
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
            ]
            await message.channel.send(random.choice(thinking_messages))

            
            # 4. Generate the Image (Gemini API Key)
            image_obj, count = await api_clients.generate_image_with_genai(
                bot_instance.gemini_client,
                enhanced_prompt,
                model="imagen-4.0-fast-generate-001" 
            )

            if image_obj and count > 0:
                # --- TRACKING (Firestore) ---
                try:
                    # This string contains "fast", so your calculator will correctly charge $0.02
                    cost = api_clients.calculate_cost("imagen-4.0-fast-generate-001", "image", count=count)
                    
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    await bot_instance.firestore_service.update_usage_stats(today, {
                        "images": count,
                        "cost": cost
                    })
                except Exception as e:
                    logging.error(f"Failed to track image cost: {e}")
                # ----------------------------

                file = discord.File(image_obj, filename="vinny_art.png")
                # NEW CODE: Save prompt in footer so we can edit it later
                embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                embed.set_image(url="attachment://vinny_art.png")
                
                # Truncate prompt to fit in footer (limit is 2048, we use 1000 to be safe)
                clean_prompt = enhanced_prompt[:1000].replace("\n", " ")
                embed.set_footer(text=f"{clean_prompt} | Requested by {message.author.display_name}")
                
                await message.channel.send(file=file, embed=embed)
                
                # --- SAFE CLEANUP FIX ---
                try:
                    # Note: We are using a BytesIO object now, so no file to delete usually,
                    # but if you modified the helper to save a file, clean it up here.
                    pass 
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup warning: {cleanup_error}")
                # ------------------------
                
                return enhanced_prompt
            
            else:
                await message.channel.send("i spilled the paint. something went wrong.")
                return None

        except Exception as e:
            logging.error(f"Image generation failed: {e}")
            await message.channel.send("my brain's fried. i can't paint right now.")
            return None
        
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