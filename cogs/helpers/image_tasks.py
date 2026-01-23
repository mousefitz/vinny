import re
import json
import logging
import discord
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

async def handle_image_request(bot_instance, message: discord.Message, image_prompt: str):
    """Rewrites the prompt, calls Imagen, and sends the result."""
    async with message.channel.typing():
        thinking_message = "aight, lemme get my brushes..."
        try:
            thinking_prompt = (f"You are Vinny, an eccentric artist. A user just asked you to paint '{image_prompt}'. Generate a very short, in-character phrase (in lowercase with typos) that you would say as you're about to start painting. Do not repeat the user's prompt. Examples: 'another masterpiece comin right up...', 'hmmm this one's gonna take some inspiration... and rum', 'aight aight i hear ya...'")
            response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=[thinking_prompt], config=bot_instance.GEMINI_TEXT_CONFIG)
            if response and response.text: 
                thinking_message = response.text.strip()
        except Exception as e: 
            logging.warning(f"Failed to generate dynamic thinking message: {e}")
            
        await message.channel.send(thinking_message)
     
        prompt_rewriter_instruction = (
                "You are a passionate, expressive artistic assistant. Your task is to turn a user's request into a visually striking image prompt.\n\n"
                "## Rules:\n"
                "1.  **MATCH THE VIBE (CRITICAL):** \n"
                "    - If the user wants something **cute/happy**, use bright lighting, soft textures, and vibrant colors.\n"
                "    - If the user wants something **scary/dark**, use heavy shadows, grit, and unsettling atmosphere.\n"
                "2.  **DO NOT SANITIZE HORROR:** If the user specifically asks for monsters, zombies, or creepy things, DO NOT water it down. Make it genuinely scary. Just do not apply this style to innocent requests.\n"
                "3.  **ENHANCE:** Add artistic details (e.g., 'cinematic lighting', 'oil painting texture') that fit the requested mood.\n\n"
                
                "## SPECIAL SUBJECTS:\n"
                "If the user asks for 'Vinny', 'yourself', 'you', or 'a self portrait', you MUST use this description:\n"
                "- **Subject:** A robust middle-aged Italian-American man with long, wild dark brown hair and a full beard.\n"
                "- **Attire:** A dark blue coat with gold toggles and a wide leather belt.\n"
                "- **Props:** Often holding a bottle of rum or a slice of pepperoni pizza.\n"
                "- **Vibe:** Chaotic, artistic, slightly drunk, pirate-like charm.\n"
                "- **Companions (Optional):** Three dogs (two light Labradors, one tan).\n\n"

                f"## User Request:\n\"{image_prompt}\"\n\n"
                "## Your Output:\n"
                "Provide your response as a single, valid JSON object with two keys: \"core_subject\" and \"enhanced_prompt\"."
            )

        smarter_prompt = image_prompt
        try:
            response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=[prompt_rewriter_instruction], config=bot_instance.GEMINI_TEXT_CONFIG)
            if response:
                json_match = re.search(r'```json\s*(\{.*?\})\s*```|(\{.*?\})', response.text, re.DOTALL)
                if json_match:
                    json_string = json_match.group(1) or json_match.group(2)
                    data = json.loads(json_string)
                    smarter_prompt = data.get("enhanced_prompt", image_prompt)
                    logging.info(f"Rewrote prompt. Core subject: '{data.get('core_subject')}'")
                else:
                    logging.warning(f"Could not find JSON in prompt rewriter response. Using original prompt.")
        except Exception: 
            logging.warning(f"Failed to rewrite image prompt, using original.", exc_info=True)

        final_prompt = smarter_prompt
        
        # Call Imagen
        image_file = await api_clients.generate_image_with_imagen(bot_instance.http_session, bot_instance.loop, final_prompt, bot_instance.GCP_PROJECT_ID, bot_instance.FIREBASE_B64)
        
        if image_file:
            response_text = "here, i made this for ya."
            try:
                image_file.seek(0)
                image_bytes = image_file.read()
                comment_prompt_text = (f"You are Vinny, an eccentric artist. You just finished painting the attached picture based on the user's request for '{image_prompt}'.\nYour task is to generate a short, single-paragraph response to show them your work. LOOK AT THE IMAGE and comment on what you ACTUALLY painted. Be chaotic, funny, or complain about it in your typical lowercase, typo-ridden style.")
                prompt_parts = [
                    types.Part(text=comment_prompt_text),
                    types.Part(inline_data=types.Blob(mime_type="image/png", data=image_bytes))
                ]
                response = await bot_instance.make_tracked_api_call(model=bot_instance.MODEL_NAME, contents=[types.Content(parts=prompt_parts)])
                if response and response.text: 
                    response_text = response.text.strip()
            except Exception: 
                logging.error("Failed to generate creative image comment.", exc_info=True)
            
            image_file.seek(0)
            await message.channel.send(response_text, file=discord.File(image_file, filename="vinny_masterpiece.png"))
        else:
            await message.channel.send("ah, crap. vinny's hands are a bit shaky today. the thing came out all wrong.")

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