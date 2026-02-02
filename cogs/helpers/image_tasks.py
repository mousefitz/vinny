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
    UPDATED: Forces the AI to pick specific 'fun facts' to drive the scene generation.
    """
    if not isinstance(target_users, list):
        target_users = [target_users]

    # 1. SETUP & CONTEXT
    if message.guild:
        guild_id = str(message.guild.id)
        server_name = message.guild.name
    else:
        guild_id = None
        server_name = "Private Context"

    logger.info(f"Starting Paint Request for {len(target_users)} users in {server_name}")

    try:
        # 2. GATHER DATA
        character_definitions = []
        
        # Extended keywords to separate physical look from personality quirks
        appearance_keywords = [
            'hair', 'eyes', 'style', 'wearing', 'build', 'height', 'look', 
            'face', 'skin', 'beard', 'glasses', 'tattoo', 'piercing', 
            'scar', 'clothes', 'clothing', 'hat', 'mask', 'gender'
        ]

        for i, user in enumerate(target_users, 1):
            appearance_facts = []
            other_facts = []
            gender_fact = None

            # --- A. SELF-PORTRAIT OVERRIDE (Vinny) ---
            if user.id == bot_instance.user.id:
                char_def = (
                    f"SUBJECT {i} (Vinny): "
                    "[[VISUALS: Robust middle-aged Italian-American man, long dark hair, messy beard, worn pirate coat or leather jacket.]] "
                    "[[TRIVIA: Chaos, Pizza, Sailing, Mechanics, Eating Trash, Dive Bars.]]"
                )
                character_definitions.append(char_def)
                continue
            
            # --- B. USER LOOKUP ---
            user_id = str(user.id)
            user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
            
            if user_profile:
                for key, value in user_profile.items():
                    clean_value = str(value).strip()
                    clean_key = key.replace('_', ' ').lower()

                    # Sanitation
                    if re.search(r'\d{17,}', clean_value) or re.search(r'<@!?&?\d+>', clean_value):
                        continue
                    
                    # Sort into Visuals vs Trivia
                    if 'gender' in clean_key:
                        gender_fact = clean_value.title()
                    elif any(keyword in clean_key for keyword in appearance_keywords): 
                        appearance_facts.append(clean_value)
                    else: 
                        other_facts.append(clean_value)

            # --- C. SUBJECT CONSTRUCTION ---
            char_str = f"SUBJECT {i} ({user.display_name}): "
            
            # 1. Visual Block
            visuals_block = []
            if gender_fact and "unknown" not in gender_fact.lower():
                visuals_block.append(f"Gender: {gender_fact}")
            
            if appearance_facts:
                visuals_block.extend(appearance_facts)
            else:
                visuals_block.append("Visuals: Undefined (You may invent a look)")
            
            char_str += f"[[VISUALS: {', '.join(visuals_block)}]] "

            # 2. Trivia Block (The Fun Details)
            if other_facts:
                random.shuffle(other_facts)
                # INCREASED LIMIT: Take 6 facts to give the AI more "fun details" to choose from
                selected_facts = other_facts[:6] 
                char_str += f"[[TRIVIA: {', '.join(selected_facts)}]]"
            else:
                char_str += "[[TRIVIA: Unknown (Invent a random, chaotic scenario)]]"

            character_definitions.append(char_str)

        # 3. USER DETAILS
        user_request = ""
        if details:
            user_request = f"USER SPECIFIC REQUEST: {details}"

        # --- 4. THE CREATIVE DIRECTOR STEP ---
        source_data = "\n".join(character_definitions)

        director_instruction = (
            "You are an expert AI Art Director. You are famous for incorporating small, specific details about people into your art.\n\n"
            "**INPUT DATA:**\n"
            f"{source_data}\n"
            f"{user_request}\n\n"
            "**YOUR TASK:** Write a detailed image generation prompt following these priorities:\n"
            "1. **VISUAL ACCURACY:** You MUST describe the characters exactly as defined in the [[VISUALS]] tags. Do not change their hair/clothes.\n"
            "2. **SCENE DETAILS (CRITICAL):** Look at the [[TRIVIA]] tags. Pick **ONE specific detail** from that list and build the entire scene around it.\n"
            "   - Example: If Trivia says 'collects stamps', show them holding a magnifying glass examining a rare stamp.\n"
            "   - Example: If Trivia says 'hates birds', show them running away from a pigeon.\n"
            "   - **DO NOT** just make them stand there. They must be engaging with their Trivia.\n"
            "3. **COMPOSITION:** Use dynamic angles (Wide shot, Action angle, Candid, Fish-eye). No boring passport photos.\n"
            "4. **ART STYLE:** Choose a unique art style (e.g. 90s Anime, Renaissance, Street Art, Cyberpunk, Claymation).\n\n"
            "**OUTPUT:** Provide ONLY the final image prompt text."
        )

        style_response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[director_instruction]
        )

        final_prompt_text = style_response.text.strip() if style_response and style_response.text else source_data
        
        logger.info(f"GENERATED DETAILED PROMPT: {final_prompt_text}")

        # 5. EXECUTE 
        await handle_image_request(
            bot_instance, 
            message, 
            final_prompt_text, 
            previous_prompt=None 
        )

    except Exception as e:
        error_msg = f"PAINT REQUEST FAILED in {server_name}: {e}"
        logger.error(error_msg, exc_info=True)
              
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
            "## CRITICAL INSTRUCTIONS:\n"
            "1. **STRICT OBEDIENCE:** You MUST include every specific action/object the user requested. If they ask for 'Vinny eating a tire', that is the focus.\n"
            "2. **STYLE:** Pick a unique style (e.g., Cyberpunk, Oil Painting, 90s Anime) unless the user requested one.\n"
            "3. **SYNC:** Your `reply_text` must describe the image you are creating.\n\n"
            f"## User Request:\n\"{image_prompt}\"\n\n"
            "## Your Output:\n"
            "Provide a single JSON object with 3 keys:\n"
            "- \"core_subject\" (Short title, e.g. 'The Pizza King')\n"
            "- \"enhanced_prompt\" (The detailed image generation prompt)\n"
            "- \"reply_text\" (Your chaotic/flirty response to the user describing what you painted)"
        )
        
        try:
            # 2. Generate Enhanced Prompt
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
                    temperature=0.7, 
                    safety_settings=safety_settings_off
                )
            )
            
            if not response or not response.text:
                await message.channel.send("my muse is on vacation. try again.")
                return None

            data = json.loads(response.text)
            # EXTRACT THE DATA
            enhanced_prompt = data.get("enhanced_prompt", image_prompt)
            core_subject = data.get("core_subject", "Artistic Chaos")
            reply_text = data.get("reply_text", "Here is that image you wanted.") # This is the new sync part

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
            # Using your specific api_clients function
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
                
                # Create the Embed
                embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                embed.set_image(url="attachment://vinny_art.png")
                clean_prompt = enhanced_prompt[:1000].replace("\n", " ")
                embed.set_footer(text=f"{clean_prompt} | Requested by {message.author.display_name}")
                
                # SEND THE REPLY TEXT AND THE IMAGE TOGETHER
                await message.channel.send(content=reply_text.lower(), file=file, embed=embed)
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
