import re
import json
import logging
import discord
import random
import os
from google.genai import types
from utils import api_clients

async def handle_paint_me_request(bot_instance, message: discord.Message):
    """Generates a prompt based on user profile and triggers image generation."""
    user_id = str(message.author.id)
    guild_id = str(message.guild.id) if message.guild else None
    user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
    
    if not user_profile:
        await message.channel.send("paint ya? i don't even know ya! tell me somethin' about yourself first with the `!vinnyknows` command.")
        return

    appearance_keywords = ['hair', 'eyes', 'style', 'wearing', 'gender', 'build', 'height', 'look']
    appearance_facts, other_facts = {}, {}
    for key, value in user_profile.items():
        if any(keyword in key.replace('_', ' ') for keyword in appearance_keywords): 
            appearance_facts[key] = value
        else: 
            other_facts[key] = value

    prompt_text = f"An artistic, masterpiece oil painting of a person named {message.author.display_name}."
    if appearance_facts:
        appearance_desc = ", ".join([f"{value}" for key, value in appearance_facts.items()])
        prompt_text += f" They are described as having {appearance_desc}."
    else:
        prompt_text += " Their appearance is unknown, so be creative."
        
    if other_facts:
        other_desc = ", ".join([f"{key.replace('_', ' ')} is {value}" for key, value in other_facts.items()])
        prompt_text += f" The painting's theme and background should be inspired by these traits: {other_desc}."
        
    await handle_image_request(bot_instance, message, prompt_text)

async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str, previous_prompt=None):
    """
    Generates an image using Gemini to rewrite the prompt and Imagen 3 to paint it.
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
            "You are a passionate, expressive artistic assistant. Your task is to turn a user's request into a visually striking image prompt.\n\n"
            "## Rules:\n"
            "1.  **MATCH THE VIBE (CRITICAL):** \n"
            "    - If the user wants something **cute/happy**, use bright lighting, soft textures, and vibrant colors.\n"
            "    - If the user wants something **scary/dark**, use heavy shadows, grit, and unsettling atmosphere.\n"
            "2.  **DO NOT SANITIZE HORROR:** If the user specifically asks for monsters, zombies, or creepy things, DO NOT water it down. Make it genuinely scary.\n"
            "3.  **ENHANCE:** Add artistic details (e.g., 'cinematic lighting', 'oil painting texture') that fit the requested mood.\n"
            f"{context_instruction}\n" # <--- Insert Context Rule Here
            "## SPECIAL SUBJECTS:\n"
            "If the user asks for 'Vinny', 'yourself', 'you', or 'a self portrait', you MUST use this description:\n"
            "- **Subject:** A robust middle-aged Italian-American man with long, wild dark brown hair and a full beard.\n"
            "- **Attire:** A dark blue coat with gold toggles and a wide leather belt.\n"
            "- **Props:** Often holding a bottle of rum or a slice of pepperoni pizza.\n"
            "- **Vibe:** Chaotic, artistic, slightly drunk, pirate-like charm.\n\n"
            f"## User Request:\n\"{image_prompt}\"\n\n"
            "## Your Output:\n"
            "Provide your response as a single, valid JSON object with two keys: \"core_subject\" and \"enhanced_prompt\"."
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
                f"aight, painting **{core_subject}**... gimme a sec.",
                f"oh i got a vision for **{core_subject}**. hold on.",
                f"mixing the paints for **{core_subject}**...",
                f"**{core_subject}**? bold choice. let's see what i can do."
            ]
            await message.channel.send(random.choice(thinking_messages))

            # 4. Generate the Image (Imagen 3)
            # FIX: We use the full API client call here instead of the simplified helper
            filename = await api_clients.generate_image_with_imagen(
                bot_instance.http_session, 
                bot_instance.loop, 
                enhanced_prompt, 
                bot_instance.GCP_PROJECT_ID, 
                bot_instance.FIREBASE_B64
            )

            if filename:
                file = discord.File(filename, filename="vinny_art.png")
                embed = discord.Embed(title=f"🎨 {core_subject.title()}", color=discord.Color.dark_teal())
                embed.set_image(url="attachment://vinny_art.png")
                embed.set_footer(text=f"Requested by {message.author.display_name}")
                
                await message.channel.send(file=file, embed=embed)
                
                # Clean up local file
                if os.path.exists(filename):
                    os.remove(filename)
                
                # RETURN the prompt so VinnyLogic can save it
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