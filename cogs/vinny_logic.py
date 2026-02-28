import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import random
import re
import logging
import contextlib
import json
import os
import aiohttp
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING
from google.genai import types
from cachetools import TTLCache

from utils import constants, api_clients
from utils.fact_extractor import extract_facts_from_message

# Helper Imports
from cogs.helpers import ai_classifiers, utilities, image_tasks, conversation_tasks

# Compile a regex for finding URLs
URL_PATTERN = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')

if TYPE_CHECKING:
    from main import VinnyBot

class VinnyLogic(commands.Cog):
    def __init__(self, bot: 'VinnyBot'):
        self.bot = bot
        # Safety settings are now in bot.GEMINI_TEXT_CONFIG, but we keep a local reference if needed
        self.memory_scheduler.start()
        self.status_rotator.start()
        self.channel_image_history = TTLCache(maxsize=100, ttl=600)
        # FIX: Prevents infinite memory leak by clearing old spam data after 5 minutes
        self.user_last_message = TTLCache(maxsize=1000, ttl=300)

    def cog_unload(self):
        self.memory_scheduler.cancel()
        self.status_rotator.cancel()

    @tasks.loop(minutes=15)
    async def status_rotator(self):
        """Rotates Vinny's Discord status to add flavor."""
        await self.bot.wait_until_ready()
        
        activities = [
            discord.Game(name="with a lighter"),
            discord.Activity(type=discord.ActivityType.watching, name="paint dry"),
            discord.Activity(type=discord.ActivityType.listening, name="the voices"),
            discord.Activity(type=discord.ActivityType.competing, name="in a bar fight"),
            discord.Game(name="don't starve"),
            discord.Activity(type=discord.ActivityType.watching, name="you"),
            discord.Activity(type=discord.ActivityType.listening, name="sea shanties"),
        ]
        
        # Pick a random one
        new_activity = random.choice(activities)
        await self.bot.change_presence(activity=new_activity, status=discord.Status.online)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        logging.error(f"Error in command '{ctx.command}':", exc_info=error)
        if isinstance(error, commands.CommandNotFound): return
        elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"eh, you missed somethin'. you need to provide the '{error.param.name}'.")
        elif isinstance(error, commands.MemberNotFound): await ctx.send(f"who? couldn't find anyone named '{error.argument}'.")
        elif isinstance(error, commands.BotMissingPermissions): await ctx.send("you're not the boss of me, but also i literally can't do that. check my permissions.")
        elif isinstance(error, commands.CommandOnCooldown): await ctx.send(f"whoa there, slow down. try again in {error.retry_after:.2f} seconds.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("heh. nice try, pal. admins only.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("you don't have the badges for that one, chief.")
        else:
            # Log the full error to console so we can debug other issues
            logging.error(f"Error in command '{ctx.command}':", exc_info=error)

    async def check_and_update_spam(self, message):
        """Checks for spam and updates the last message memory. Returns (is_duplicate, is_rapid)."""
        user_id = str(message.author.id)
        content = message.content.strip().lower()
        current_time = datetime.datetime.now()
        
        is_duplicate_spam = False
        is_rapid_fire = False
        
        last_msg = self.user_last_message.get(user_id)
        if last_msg:
            time_diff = (current_time - last_msg['time']).total_seconds()
            
            # Check 1: Did they copy-paste the exact same thing recently? (within 60s)
            # You can change '60' to '300' if you want a 5-minute spam block!
            if last_msg['content'] == content and time_diff < 60:
                is_duplicate_spam = True
            # Check 2: Are they rapid-firing different messages? (less than 5 seconds apart)
            elif time_diff < 5:
                is_rapid_fire = True
                
        self.user_last_message[user_id] = {'content': content, 'time': current_time}
        return is_duplicate_spam, is_rapid_fire

    async def handle_relationship(self, message, sentiment_analysis, is_rapid=False):
        """Determines the score change based on sentiment."""
        user_id = str(message.author.id)
        guild_id = str(message.guild.id) if message.guild else "dm_context"

        if is_rapid:
            return # Rapid fire typing gets no points to prevent farming
            
        # Normal conversation! Award points based on sentiment.
        score_change = 0
        
        # --- THE FIX: Safely check if it's a dict or a string ---
        if isinstance(sentiment_analysis, dict):
            sentiment = sentiment_analysis.get('sentiment', 'neutral').lower()
        else:
            sentiment = str(sentiment_analysis).lower()
            
        if sentiment == 'positive': 
            score_change = 1
        elif sentiment == 'negative': 
            score_change = -1
        
        if score_change != 0:
            await self.bot.firestore_service.update_relationship_score(
                user_id, guild_id, score_change
            )
                
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        bot_names = ["vinny", "vincenzo", "vin vin"]
        
        # 1. Basic Filters
        if message.author.bot or message.id in self.bot.processed_message_ids or message.content.startswith(self.bot.command_prefix): 
            return
        self.bot.processed_message_ids[message.id] = True

        # --- NEW: TRACK MESSAGE COUNT ---
        if message.guild:
            asyncio.create_task(self.bot.firestore_service.increment_message_count(
                str(message.author.id), str(message.guild.id)
            ))

        try:
            # 2. Fix Embeds (Twitter/TikTok links)
            if await utilities.check_and_fix_embeds(message): return
            
            # 3. Clean Content
            cleaned_content = re.sub(f'<@!?{self.bot.user.id}>', '', message.content).strip()
            msg_content_lower = message.content.lower()

            # 4. Check for Corrections
            if await ai_classifiers.is_a_correction(self.bot, message, self.bot.GEMINI_TEXT_CONFIG):
                return await conversation_tasks.handle_correction(self.bot, message)
            
            # =========================================================================
            # NEW: URL SUMMARIZATION
            # =========================================================================
            summary_triggers = ["summarize", "summary", "tldr", "tl;dr", "give me the gist", "what's this about", "break it down"]
            is_summary_request = any(trigger in msg_content_lower for trigger in summary_triggers)
            is_addressed = "vinny" in msg_content_lower or self.bot.user in message.mentions or (message.reference and message.reference.resolved and message.reference.resolved.author == self.bot.user)

            if is_summary_request and is_addressed:
                target_url = None
                urls = URL_PATTERN.findall(message.content)
                if urls: target_url = urls[0]
                elif message.reference and message.reference.resolved:
                    replied_msg = message.reference.resolved
                    replied_urls = URL_PATTERN.findall(replied_msg.content)
                    if replied_urls: target_url = replied_urls[0]
                    elif replied_msg.embeds:
                        for embed in replied_msg.embeds:
                            if embed.url:
                                target_url = embed.url
                                break
                if not target_url:
                    async for past_msg in message.channel.history(limit=5):
                        past_urls = URL_PATTERN.findall(past_msg.content)
                        if past_urls:
                            target_url = past_urls[0]
                            break
                if target_url:
                    async with message.channel.typing():
                        summary = await conversation_tasks.summarize_url(self.bot, self.bot.http_session, target_url)
                        await message.reply(summary)
                    return 

            # =========================================================================
            # 1. HANDLING REPLIES (Priority Logic)
            # =========================================================================
            if message.reference:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    
                    # --- GLOBAL CHECK: IS THIS AN IMAGE EDIT REQUEST? ---
                    is_reply_to_vinny = (ref_msg.author.id == self.bot.user.id)
                    should_check_edit = is_reply_to_vinny or is_addressed
                    
                    # Check if there is an image to edit (Attachment or Embed)
                    has_image = (ref_msg.attachments or ref_msg.embeds)
                    
                    if should_check_edit and has_image:
                        # --- 1. STRICT COMMAND TRIGGERS ---
                        trigger_words = ["add", "change", "remove", "draw", "paint", "edit", "fix", "remix", "modify", "crop", "resize"]
                        
                        clean_lower = re.sub(r'\b(vinny|vincenzo|vin|bot)\b', '', cleaned_content.lower()).strip()
                        clean_lower = re.sub(r'^[^a-z0-9]+', '', clean_lower).strip()
                        first_word = clean_lower.split(' ')[0] if clean_lower else ""
                        
                        # CHECK 1: Forced command?
                        is_edit = (first_word in trigger_words)
                        
                        # CHECK 2: AI Judge?
                        if not is_edit: 
                            try:
                                is_edit = await ai_classifiers.is_image_edit_request(self.bot, cleaned_content)
                            except: is_edit = False

                        # --- EXECUTE EDIT ---
                        if is_edit:
                            logging.info(f"🎨 EDIT DETECTED: '{cleaned_content}'")
                            async with message.channel.typing():
                                
                                # Download Image
                                input_image_bytes = None
                                if ref_msg.attachments:
                                    for att in ref_msg.attachments:
                                        if (att.content_type and "image" in att.content_type) or att.height:
                                            input_image_bytes = await att.read()
                                            break
                                elif ref_msg.embeds and ref_msg.embeds[0].image:
                                    try:
                                        import aiohttp
                                        async with aiohttp.ClientSession() as session:
                                            async with session.get(ref_msg.embeds[0].image.url) as resp:
                                                if resp.status == 200: input_image_bytes = await resp.read()
                                    except: pass

                                # Get Previous Prompt
                                previous_prompt = None
                                if ref_msg.embeds and ref_msg.embeds[0].footer.text:
                                    footer_text = ref_msg.embeds[0].footer.text
                                    if "|" in footer_text: previous_prompt = footer_text.split("|")[0].strip()
                                if not previous_prompt:
                                    previous_prompt = self.channel_image_history.get(message.channel.id)

                                # --- DECISION: STANDARD EDIT OR PORTRAIT INJECTION? ---
                                # THE FIX IS HERE: Added '|my' to the regex so "my cats" triggers lookup.
                                is_self_ref = re.search(r'\b(me|myself|i|my)\b', cleaned_content, re.IGNORECASE)
                                mentions = [m for m in message.mentions if m.id != self.bot.user.id]

                                # If "Add me/my X" or "Add @User", use the Portrait System
                                if (is_self_ref or mentions) and "add" in cleaned_content.lower():
                                    target_users = []
                                    if is_self_ref: target_users.append(message.author)
                                    target_users.extend(mentions)
                                    target_users = list(set(target_users)) 
                                    
                                    # Routes to the function that looks up pets/appearance
                                    await image_tasks.handle_portrait_request(
                                        self.bot, 
                                        message, 
                                        target_users, 
                                        details=cleaned_content,
                                        previous_prompt=previous_prompt,
                                        input_image_bytes=input_image_bytes
                                    )
                                else:
                                    # Standard Edit (e.g. "Change the background")
                                    await image_tasks.handle_image_request(
                                        self.bot, 
                                        message, 
                                        cleaned_content, 
                                        previous_prompt=previous_prompt, 
                                        input_image_bytes=input_image_bytes
                                    )
                            return # STOP HERE. Vinny paints and leaves.

                    # --- IF NOT AN EDIT, HANDLE AS CHAT ---
                    if is_reply_to_vinny or is_addressed:
                        await self.update_vinny_mood()
                        async with message.channel.typing():
                            await conversation_tasks.handle_direct_reply(self.bot, message)
                        return

                except Exception as e:
                    logging.error(f"Error handling reply context: {e}")

            # 5. Handle Context Replying (Pinging an image without text)
            if not cleaned_content and self.bot.user.mentioned_in(message): 
                async for last_message in message.channel.history(limit=1, before=message):
                    if last_message.attachments and "image" in last_message.attachments[0].content_type:
                        return await image_tasks.handle_image_reply(self.bot, message, last_message)
            
            # =========================================================================
            # 2. AUTONOMOUS & GENERAL CHAT
            # =========================================================================
            should_respond, is_autonomous = False, False
            if self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in bot_names):
                should_respond = True
            elif self.bot.autonomous_mode_enabled and message.guild and random.random() < self.bot.autonomous_reply_chance:
                should_respond, is_autonomous = True, True
            elif message.guild is None:
                should_respond = True

            if should_respond:
                # --- PASSIVE LEARNING ---
                if self.bot.PASSIVE_LEARNING_ENABLED and message.attachments:
                    image_bytes, mime_type = None, None
                    for att in message.attachments:
                        if "image" in att.content_type and att.size < 8 * 1024 * 1024:
                            image_bytes = await att.read()
                            mime_type = att.content_type
                            break
                    
                    if image_bytes:
                        if extracted_facts := await extract_facts_from_message(self.bot, message, author_name=None, image_bytes=image_bytes, mime_type=mime_type):
                            for key, value in extracted_facts.items():
                                await self.bot.firestore_service.save_user_profile_fact(str(message.author.id), str(message.guild.id) if message.guild else None, key, value)
                                logging.info(f"👁️ Learned visual fact: {key}={value}")

                # --- DETERMINE INTENT ---
                intent, args = await ai_classifiers.get_intent_from_prompt(self.bot, message)
                if message.attachments and intent == "tag_user":
                    logging.info("🖼️ Image detected: Overriding 'tag_user' intent to 'respond_to_image'.")
                    intent = None
                typing_ctx = message.channel.typing() if not is_autonomous else contextlib.nullcontext()
                
                async with typing_ctx:
                    if intent == "generate_image":
                        raw_prompt = args.get("prompt", cleaned_content)
                        clean_prompt = re.sub(r'\b(vinny|vincenzo|vin|draw|paint|make|generate|please)\b', '', raw_prompt, flags=re.IGNORECASE).strip()
                        if len(clean_prompt) < 2: clean_prompt = raw_prompt
                        previous_prompt = self.channel_image_history.get(message.channel.id)
                        final_prompt = await image_tasks.handle_image_request(self.bot, message, clean_prompt, previous_prompt)
                        if final_prompt: self.channel_image_history[message.channel.id] = final_prompt
                    
                    elif intent == "search_google_images":
                        search_query = args.get("query") or cleaned_content
                        search_query = re.sub(r'\b(find|search|look for|picture of|photo of|google)\b', '', search_query, flags=re.IGNORECASE).strip()
                        if not search_query: await message.reply("ya gotta tell me what to look for, pal.")
                        else:
                            results = await api_clients.search_google_images(self.bot.http_session, self.bot.SERPER_API_KEY, search_query)
                            if results:
                                view = utilities.ImagePaginator(results, search_query, message.author)
                                await message.reply(embed=view.get_embed(), view=view)
                            else: await message.reply("i looked everywhere, nothin'.")

                    elif intent == "generate_user_portrait": 
                        target_str = args.get("target", "me")
                        details = args.get("details", "")
                        identified_users = []
                        for m in message.mentions:
                            if m not in identified_users: identified_users.append(m)
                        clean_str = re.sub(r'<@!?\d+>', '', target_str).lower()
                        potential_names = re.split(r'\s+(?:and|&|,|with)\s+', clean_str)
                        for name in potential_names:
                            name = name.strip()
                            if not name: continue
                            if name.lower() in ["vinny", "vincenzo", "vin", "draw", "paint", "picture", "of", "please"]: continue
                            if name in ["me", "myself", "i"]:
                                if message.author not in identified_users: identified_users.append(message.author)
                            else:
                                found = discord.utils.find(lambda m: name in m.display_name.lower() or name in m.name.lower(), message.guild.members)
                                if not found: found = await utilities.find_user_by_vinny_name(self.bot, message.guild, name)
                                if found:
                                    if found.id == self.bot.user.id:
                                        is_explicit = re.search(r'\b(yourself|self|us|we)\b', message.content.lower())
                                        if not is_explicit: continue 
                                    if found not in identified_users: identified_users.append(found)
                        if not identified_users:
                            if target_str.lower() in ["yourself", "self", "vinny"]: identified_users.append(self.bot.user)
                            else:
                                identified_users.append(message.author)
                                if target_str.lower() not in ['me', 'myself']: await message.channel.send(f"couldn't find '{target_str}', so i'm just paintin' you.")
                        await image_tasks.handle_portrait_request(self.bot, message, identified_users, details)

                    elif intent == "get_user_knowledge":
                        target_user_name = args.get("target_user", "")
                        target_user = None
                        valid_mentions = [m for m in message.mentions if m.id != self.bot.user.id]
                        if valid_mentions: target_user = valid_mentions[0]
                        clean_target = target_user_name.lower().strip()
                        if not target_user and (not clean_target or clean_target in ["me", "myself", "i", "user", "the user", "self", "my profile"]): target_user = message.author
                        elif not target_user and message.guild:
                            search_name = target_user_name.replace("@", "").strip()
                            target_user = discord.utils.find(lambda m: search_name.lower() in m.display_name.lower(), message.guild.members)
                            if not target_user: target_user = await utilities.find_user_by_vinny_name(self.bot, message.guild, search_name)
                        if target_user: await conversation_tasks.handle_knowledge_request(self.bot, message, target_user)
                        else: await message.channel.send(f"who? i looked all over, couldn't find anyone named '{target_user_name}'.")

                    elif intent == "tag_user":
                        user_to_tag = args.get("user_to_tag")
                        times = args.get("times_to_tag", 1)
                        if user_to_tag: await conversation_tasks.find_and_tag_member(self.bot, message, user_to_tag, times)
                        else: await message.channel.send("ya gotta tell me who to tag, pal.")
                    
                    elif intent == "get_my_name":
                         user_name_to_use = await self.bot.firestore_service.get_user_nickname(str(message.author.id)) or message.author.display_name
                         await message.channel.send(f"your name? i call ya '{user_name_to_use}'.")

                    else: 
                        # --- SPAM CHECK BEFORE RESPONDING ---
                        is_duplicate, is_rapid = await self.check_and_update_spam(message)
                        
                        if is_duplicate:
                            try: await message.add_reaction("😠") 
                            except: pass
                            
                            # The AI Beatdown Prompt!
                            beatdown_prompt = (
                                f"# TASK:\n"
                                f"The user '{message.author.display_name}' just copy-pasted the exact same message to try and farm relationship points with you. "
                                f"Their spam message was: \"{message.content}\"\n\n"
                                f"Give them a short, highly creative, angry verbal beatdown telling them to stop spamming. "
                                f"Mock them for trying to cheat the system. Do NOT answer their original prompt."
                            )
                            try:
                                resp = await self.bot.make_tracked_api_call(model=self.bot.MODEL_NAME, contents=[beatdown_prompt], config=self.bot.GEMINI_TEXT_CONFIG)
                                if resp and resp.text: await message.reply(resp.text.strip())
                            except:
                                await message.reply("stop copy-pasting the same garbage, pal. i ain't givin' you points for that.")
                            return # Stop processing the message!

                        # If not duplicate spam, process normally
                        async def update_sentiment_background():
                            try:
                                user_sentiment = await ai_classifiers.get_message_sentiment(self.bot, message.content)
                                await self.update_mood_based_on_sentiment(user_sentiment)
                                await self.update_vinny_mood()
                                # Pass the 'is_rapid' flag so he knows not to award points
                                await self.handle_relationship(message, user_sentiment, is_rapid=is_rapid)
                            except Exception as e: logging.error(f"Background mood update failed: {e}")
                        asyncio.create_task(update_sentiment_background())
                        
                        await conversation_tasks.handle_text_or_image_response(
                            self.bot, message, is_autonomous=is_autonomous, summary=None
                        )
            else:
                explicit_reaction_keywords = ["react to this", "add an emoji", "emoji this", "react vinny"]
                if "pie" in message.content.lower() and random.random() < 0.75: await message.add_reaction('🥧')
                elif any(keyword in message.content.lower() for keyword in explicit_reaction_keywords) or (random.random() < self.bot.reaction_chance):
                    try:
                        emoji = random.choice(message.guild.emojis) if message.guild and message.guild.emojis else random.choice(['😂', '👍', '👀', '🍕', '🍻', '🥃', '🐶', '🎨'])
                        await message.add_reaction(emoji)
                    except Exception: pass
                return

        except Exception:
            logging.critical("CRITICAL ERROR in on_message", exc_info=True)
            
    # --- MOOD LOGIC ---

    async def update_mood_based_on_sentiment(self, sentiment: str):
        """Influences Vinny's mood based on conversational sentiment."""
        mood_map = {
            "positive": ["cheerful", "flirty"],
            "negative": ["cranky", "depressed", "belligerent"],
            "sarcastic": ["suspicious", "cranky"],
            "flirty": ["flirty", "horny"],
            "angry": ["belligerent", "cranky"],
        }
        if sentiment in mood_map and random.random() < 0.25: 
            new_mood = random.choice(mood_map[sentiment])
            if self.bot.current_mood != new_mood:
                self.bot.current_mood = new_mood
                self.bot.last_mood_change_time = datetime.datetime.now()
                logging.info(f"Vinny's mood was influenced by conversation. New mood: {self.bot.current_mood}")

    async def update_vinny_mood(self):
        if datetime.datetime.now() - self.bot.last_mood_change_time > self.bot.MOOD_CHANGE_INTERVAL:
            self.bot.current_mood = random.choice([m for m in self.bot.MOODS if m != self.bot.current_mood])
            self.bot.last_mood_change_time = datetime.datetime.now()
            logging.info(f"Vinny's mood has changed to: {self.bot.current_mood}")

    @tasks.loop(minutes=30)
    async def memory_scheduler(self):
        """Run summary logic only for channels that had activity in the last 30 mins."""
        await self.bot.wait_until_ready()
        logging.info("Memory scheduler starting...")
        
        for guild in self.bot.guilds:
            messages = []
            for channel in guild.text_channels:
                if not channel.permissions_for(guild.me).read_message_history:
                    continue

                try:
                    # 1. OPTIMIZATION: Check strict recency first to avoid API spam
                    # Fetch just 1 message to check activity
                    last_msg = None
                    async for m in channel.history(limit=1):
                        last_msg = m
                        break
                    
                    if not last_msg:
                        continue # Empty channel

                    # Check time difference (30 mins)
                    time_diff = datetime.datetime.now(datetime.UTC) - last_msg.created_at
                    if time_diff > datetime.timedelta(minutes=30):
                        continue # No recent activity, skip fetching history
                    
                    # 2. If recent, fetch full history for summary
                    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
                    async for message in channel.history(limit=100, after=since):
                        if not message.author.bot:
                            messages.append({"author": message.author.display_name, "content": message.content, "timestamp": message.created_at.isoformat()})
                
                except discord.Forbidden: continue
                except Exception as e: logging.error(f"Could not fetch history for channel '{channel.name}': {e}")
            
            if len(messages) > 5:
                logging.info(f"Generating summary for guild '{guild.name}' with {len(messages)} messages.")
                messages.sort(key=lambda x: x['timestamp'])
                if summary_data := await conversation_tasks.generate_memory_summary(self.bot, messages):
                    await self.bot.firestore_service.save_memory(str(guild.id), summary_data)
                    logging.info(f"Saved memory summary for guild '{guild.name}'.")
                    
        logging.info("Memory scheduler finished.")

    # --- BOT COMMANDS ---
    
    @commands.command(name='help')
    async def help_command(self, ctx):
        embed = discord.Embed(title="What do ya want?", description="Heh. Aight, so you need help? Pathetic. Here's the stuff I can do if ya use the '!' thing. Don't get used to it.", color=discord.Color.dark_gold())
        
        # --- General Commands ---
        embed.add_field(name="!vinnyknows [fact]", value="Teaches me somethin' about you. spill the beans.\n*Example: `!vinnyknows my favorite color is blue`*", inline=False)
        embed.add_field(name="!vibe [@user]", value="Checks what I think of you (or someone else if you tag 'em).", inline=False)
        embed.add_field(name="!leaderboard", value="Shows the server leaderboards (The Vibe List and The Earaches).", inline=False)
        embed.add_field(name="!rolecolor [hex1] [hex2]", value="Sets your custom role color (and optional gradient).\n*Example: `!rolecolor #FF0000 #0000FF`*", inline=False)
        embed.add_field(name="!rolename [new name]", value="Renames your custom color role.\n*Example: `!rolename The Big Cheese`*", inline=False)
        embed.add_field(name="!forgetme", value="Makes me forget everything I know about you *in this server*.", inline=False)
        embed.add_field(name="!weather [location]", value="Gives you the damn weather. Don't blame me if it's wrong.\n*Example: `!weather 90210`*", inline=False)
        embed.add_field(name="!horoscope [sign]", value="I'll look at the sky and tell ya what's up. It's probably chaos.\n*Example: `!horoscope gemini`*", inline=False)
        
        # --- Marriage Commands ---
        embed.add_field(name="!propose [@user]", value="Get down on one knee and propose to someone special.", inline=False)
        embed.add_field(name="!marry [@user]", value="Accept a proposal from someone who just proposed to you.", inline=False)
        embed.add_field(name="!divorce", value="End your current marriage. Ouch.", inline=False)
        embed.add_field(name="!ballandchain", value="Checks who you're hitched to. If you have to ask, it might be bad news.", inline=False)
        embed.add_field(name="!vinnycalls [@user] [name]", value="Gives someone a nickname that I'll remember.\n*Example: `!vinnycalls @SomeUser Cori`*", inline=False)
        
        # --- Admin / Owner Commands ---
        is_admin = ctx.channel.permissions_for(ctx.author).manage_guild or await self.bot.is_owner(ctx.author)
        
        if is_admin:
            embed.add_field(name="----------------", value="**👑 BOSS COMMANDS 👑**", inline=False)
            embed.add_field(name="!setup_rolecolor [#channel] [@role]", value="**(Admin)** Sets the allowed channel and anchor role for !rolecolor.", inline=False)
            embed.add_field(name="!sync_messages", value="**(Admin)** Scans the server history to backfill The Earaches leaderboard.", inline=False)
            
        if await self.bot.is_owner(ctx.author):
            embed.add_field(name="!vinnycost", value="**(Owner Only)** Checks the daily bill. See how much cash I'm burning.", inline=False)
            embed.add_field(name="!autonomy [on/off]", value="**(Owner Only)** Turns my brain on or off. Lets me talk without bein' talked to.", inline=False)
            embed.add_field(name="!set_relationship [@user] [score]", value="**(Owner Only)** Sets the numeric relationship score manually.", inline=False)
            embed.add_field(name="!forgive_all", value="**(Owner Only)** Resets EVERYONE'S relationship score to 0 (Neutral).", inline=False)
            embed.add_field(name="!clear_memories", value="**(Owner Only)** Clears all of my automatic conversation summaries for this server.", inline=False)
            
        embed.set_footer(text="Now stop botherin' me. Salute!")
        await ctx.send(embed=embed)

    @commands.command(name='vinnycalls')
    async def vinnycalls_command(self, ctx, member: discord.Member, nickname: str):
        if await self.bot.firestore_service.save_user_nickname(str(member.id), nickname):
            await ctx.send(f"aight, got it. callin' {member.mention} '{nickname}'.")

    @commands.command(name='forgetme')
    async def forgetme_command(self, ctx):
        if not ctx.guild: return await ctx.send("this only works in a server, pal.")
        if await self.bot.firestore_service.delete_user_profile(str(ctx.author.id), str(ctx.guild.id)):
            await ctx.send(f"aight, {ctx.author.mention}. i scrambled my brains. who are you again?")
        else:
            await ctx.send("my head's already empty, pal.")

    @commands.command(name='propose')
    async def propose_command(self, ctx, member: discord.Member):
        if ctx.author == member: 
            return await ctx.send("heh, cute. but you can't propose to yourself, pal. pick someone else.")
        elif member == self.bot.user:
            return await ctx.send("whoa there, i'm flattered, really. but my heart belongs to the sea... and maybe rum. i'm off the market, sweetie.")
        elif member.bot: 
            return await ctx.send("proposin' to a robot? find a real pulse.")
        if await self.bot.firestore_service.save_proposal(str(ctx.author.id), str(member.id)):
            await ctx.send(f"whoa! {ctx.author.mention} is on one knee for {member.mention}. you got 5 mins to say yes with `!marry @{ctx.author.display_name}`.")

    @commands.command(name='marry')
    async def marry_command(self, ctx, member: discord.Member):
        if not await self.bot.firestore_service.check_proposal(str(member.id), str(ctx.author.id)):
            return await ctx.send(f"{member.display_name} didn't propose to you.")
        if await self.bot.firestore_service.finalize_marriage(str(member.id), str(ctx.author.id)):
            await ctx.send(f":tada: they said yes! i now pronounce {member.mention} and {ctx.author.mention} hitched!")

    @commands.command(name='divorce')
    async def divorce_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        if not profile or "married_to" not in profile: return await ctx.send("you ain't married.")
        partner_id = profile["married_to"]
        if await self.bot.firestore_service.process_divorce(str(ctx.author.id), partner_id):
            await ctx.send(f"it's over. {ctx.author.mention} has split from <@{partner_id}>. 📜")

    @commands.command(name='ballandchain')
    async def ballandchain_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        if profile and profile.get("married_to"):
            partner_id = profile.get("married_to")
            try:
                partner = await self.bot.fetch_user(int(partner_id))
                partner_name = partner.display_name if partner else "a ghost"
            except (discord.NotFound, ValueError):
                partner_name = "a ghost I can't find anymore"
            await ctx.send(f"you're shackled to **{partner_name}**. happened on **{profile.get('marriage_date', 'some forgotten day')}**.")
        else:
            await ctx.send("you ain't married to nobody.")

    @commands.command(name='weather')
    async def weather_command(self, ctx, *, location: str):
        async with ctx.typing():
            coords = await api_clients.geocode_location(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, location)
            if not coords:
                return await ctx.send(f"eh, couldn't find that place '{location}'. you sure that's a real place?")
            current_weather_data = await api_clients.get_weather_data(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, coords['lat'], coords['lon'])
            forecast_data = await api_clients.get_5_day_forecast(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, coords['lat'], coords['lon'])

        if not current_weather_data:
            return await ctx.send("found the place but the damn current weather report is all garbled.")

        city_name = coords.get("name", "Unknown Location")
        embeds = []
        try:
            main_weather = current_weather_data["weather"][0]
            emoji = constants.get_weather_emoji(main_weather['main'])
            
            embed1 = discord.Embed(title=f"{emoji} Weather in {city_name}", description=f"**{main_weather.get('description', '').title()}**", color=discord.Color.blue())
            embed1.add_field(name="🌡️ Now", value=f"{current_weather_data['main'].get('temp'):.0f}°F", inline=True)
            embed1.add_field(name="🔼 High", value=f"{current_weather_data['main'].get('temp_max'):.0f}°F", inline=True)
            embed1.add_field(name="🔽 Low", value=f"{current_weather_data['main'].get('temp_min'):.0f}°F", inline=True)
            embed1.add_field(name="🤔 Feels Like", value=f"{current_weather_data['main'].get('feels_like'):.0f}°F", inline=True)
            embed1.add_field(name="💧 Humidity", value=f"{current_weather_data['main'].get('humidity')}%", inline=True)
            embed1.add_field(name="💨 Wind", value=f"{current_weather_data['wind'].get('speed'):.0f} mph", inline=True)
            embed1.add_field(name="📡 Live Radar", value=f"[Click to View](https://www.windy.com/{coords['lat']}/{coords['lon']})", inline=False)
            embed1.set_footer(text="Page 1 of 2 | don't blame me if the sky starts lyin'. salute!")
            embeds.append(embed1)
        except (KeyError, IndexError):
            return await ctx.send("failed to parse the current weather data. weird.")

        if forecast_data and forecast_data.get("list"):
            try:
                embed2 = discord.Embed(title=f"🗓️ 5-Day Forecast for {city_name}", color=discord.Color.dark_blue())
                daily_forecasts = {}
                for entry in forecast_data["list"]:
                    day = datetime.datetime.fromtimestamp(entry['dt']).strftime('%Y-%m-%d')
                    if day not in daily_forecasts: daily_forecasts[day] = {'highs': [], 'lows': [], 'icons': []}
                    daily_forecasts[day]['highs'].append(entry['main']['temp_max'])
                    daily_forecasts[day]['lows'].append(entry['main']['temp_min'])
                    daily_forecasts[day]['icons'].append(entry['weather'][0]['main'])
                
                day_keys = sorted(daily_forecasts.keys())
                for day in day_keys[:5]:
                    day_name = datetime.datetime.strptime(day, '%Y-%m-%d').strftime('%A')
                    high = max(daily_forecasts[day]['highs'])
                    low = min(daily_forecasts[day]['lows'])
                    most_common_icon = max(set(daily_forecasts[day]['icons']), key=daily_forecasts[day]['icons'].count)
                    emoji = constants.get_weather_emoji(most_common_icon)
                    embed2.add_field(name=f"**{day_name}**", value=f"{emoji} {high:.0f}°F / {low:.0f}°F", inline=False)
                embed2.set_footer(text="Page 2 of 2 | don't blame me if the sky starts lyin'. salute!")
                embeds.append(embed2)
            except Exception:
                logging.error("Failed to parse 5-day forecast data.", exc_info=True)

        class WeatherView(discord.ui.View):
            def __init__(self, embeds):
                super().__init__(timeout=60)
                self.embeds = embeds
                self.current_page = 0
                if len(self.embeds) < 2: self.children[1].disabled = True
                
            @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey, disabled=True)
            async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user != ctx.author:
                    return await interaction.response.send_message("get your own weather report, pal.", ephemeral=True)
                self.current_page -= 1
                await self.update_message(interaction)
                
            @discord.ui.button(label="Next", style=discord.ButtonStyle.blurple)
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user != ctx.author:
                    return await interaction.response.send_message("get your own weather report, pal.", ephemeral=True)
                self.current_page += 1
                await self.update_message(interaction)
                
            async def update_message(self, interaction: discord.Interaction):
                self.children[0].disabled = self.current_page == 0
                self.children[1].disabled = self.current_page >= len(self.embeds) - 1
                await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

        if embeds: await ctx.send(embed=embeds[0], view=WeatherView(embeds))
        else: await ctx.send("somethin' went wrong with the damn weather machine.")

    @commands.command(name='horoscope')
    async def horoscope_command(self, ctx, *, sign: str):
        valid_signs = list(constants.SIGN_EMOJIS.keys())
        clean_sign = sign.lower()
        if clean_sign not in valid_signs: 
            return await ctx.send(f"'{sign}'? that ain't a star sign, pal. try one of these: {', '.join(valid_signs)}.")
        
        # --- BULLETPROOF CACHE SETUP ---
        if not hasattr(self, 'horoscope_cache'):
            self.horoscope_cache = {"date": None, "data": {}}
            
        import datetime
        current_time = datetime.datetime.now()
        today_date_str = current_time.strftime('%Y-%m-%d')
        
        # Wipe the cache if it's a new day
        if self.horoscope_cache["date"] != today_date_str:
            self.horoscope_cache = {"date": today_date_str, "data": {}}
            
        async with ctx.typing():
            # Serve from cache if we already wrote it today
            if clean_sign in self.horoscope_cache["data"]:
                vinnyfied_text = self.horoscope_cache["data"][clean_sign]
            else:
                # Generate a new one if it's the first time today
                vinnyfied_text = "the stars are all fuzzy today. couldn't get a readin'. maybe they're drunk."
                try:
                    import aiohttp
                    raw_api_data = "Astrology data unavailable today."
                    
                    async with aiohttp.ClientSession() as session:
                        api_url = f"https://freehoroscopeapi.com/api/v1/get-horoscope/daily?sign={clean_sign}&day=today"
                        async with session.get(api_url) as resp:
                            if resp.status == 200:
                                raw_api_data = await resp.text() 
                                
                    prompt = (
                        f"# --- YOUR TASK ---\n"
                        f"You are giving a daily horoscope reading for the sign **{clean_sign.title()}**.\n"
                        f"Here is the actual, real astrological data for today:\n"
                        f"```json\n{raw_api_data}\n```\n\n"
                        f"## INSTRUCTIONS:\n"
                        f"Rewrite the core meaning of that exact horoscope data into a short, single-paragraph daily reading in your unique, chaotic, flirty, and slightly unhinged voice. "
                        f"Keep the astrological themes the same, but completely change the wording so it sounds like YOU."
                    )
                    response = await self.bot.make_tracked_api_call(model=self.bot.MODEL_NAME, contents=[prompt], config=self.bot.GEMINI_TEXT_CONFIG)
                    
                    if response and response.text: 
                        vinnyfied_text = response.text.strip()
                        self.horoscope_cache["data"][clean_sign] = vinnyfied_text
                        
                except Exception as e: 
                    import logging
                    logging.error(f"Failed to generate Vinny horoscope: {e}")

            # Send the embed!
            import discord
            emoji = constants.SIGN_EMOJIS.get(clean_sign, "✨")
            embed = discord.Embed(title=f"{emoji} Horoscope for {clean_sign.title()}", description=vinnyfied_text, color=discord.Color.dark_purple())
            embed.set_thumbnail(url="https://i.imgur.com/4laks52.gif")
            embed.set_footer(text="don't blame me if the stars lie. they're drama queens.")
            await ctx.send(embed=embed)
            
    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        target_user = ctx.author
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]
            knowledge_string = re.sub(r'<@!?\d+>', '', knowledge_string).strip()

        extracted_facts = await extract_facts_from_message(self.bot, knowledge_string, author_name=target_user.display_name)
        if not extracted_facts:
            logging.warning(f"Fact extraction failed for string: '{knowledge_string}'")
            await ctx.send("eh? what're you tryin' to tell me? i didn't get that. try sayin' it like 'my favorite food is pizza'.")
            return

        saved_facts = []
        guild_id = str(ctx.guild.id) if ctx.guild else None
        for key, value in extracted_facts.items():
            if await self.bot.firestore_service.save_user_profile_fact(str(target_user.id), guild_id, key, value):
                saved_facts.append(f"'{key}' is '{value}'")

        if saved_facts:
            facts_confirmation = ", ".join(saved_facts)
            target_name = "themselves" if target_user == ctx.author else target_user.display_name
            confirmation_prompt = (
                f"# --- YOUR TASK ---\n"
                f"A user just taught you a fact. Your task is to confirm that you've learned it in your own chaotic, reluctant, or flirty way. Obey all your personality directives.\n\n"
                f"## CONTEXT:\n"
                f"- **The Teacher:** '{ctx.author.display_name}'\n"
                f"- **The Subject of the Fact:** '{target_name}'\n"
                f"- **The Fact Itself:** {facts_confirmation}\n\n"
                f"## INSTRUCTIONS:\n"
                f"1.  First, combine the **Subject** and the **Fact** into a complete thought (e.g., 'enraged smells like poo').\n"
                f"2.  Then, generate a short, lowercase, typo-ridden confirmation that shows you understand this complete thought. Acknowledge that **The Teacher** taught you this."
            )
            try:
                response = await self.bot.make_tracked_api_call(model=self.bot.MODEL_NAME, contents=[confirmation_prompt], config=self.bot.GEMINI_TEXT_CONFIG)
                if response and response.text: await ctx.send(response.text.strip())
                else: raise Exception("API call failed or returned no text.")
            except Exception:
                logging.error("Failed to generate dynamic confirmation for !vinnyknows.", exc_info=True)
                await ctx.send(f"aight, i got it. so {'your' if target_user == ctx.author else f'{target_user.display_name}\'s'} {facts_confirmation}. vinny will remember.")
        else:
            await ctx.send("my head's all fuzzy. tried to remember that but it slipped out.")

    @commands.command(name='autonomy')
    @commands.is_owner()
    async def autonomy_command(self, ctx, status: str):
        if status.lower() == 'on': self.bot.autonomous_mode_enabled = True; await ctx.send("aight, vinny's off the leash.")
        elif status.lower() == 'off': self.bot.autonomous_mode_enabled = False; await ctx.send("thank god. vinny's back in his cage.")
        else: await ctx.send("it's 'on' or 'off', pal. pick one.")

    @commands.command(name='set_relationship')
    @commands.is_owner()
    async def set_relationship_command(self, ctx, member: discord.Member, score: float):
        """Sets a user's relationship score directly. Status updates automatically."""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        user_id = str(member.id)
        
        # 1. Update the score
        if await self.bot.firestore_service.save_user_profile_fact(user_id, guild_id, 'relationship_score', score):
            # 2. Force status update
            await conversation_tasks.update_relationship_status(self.bot, user_id, guild_id, score)
            
            # 3. Retrieve the new status to confirm to the user
            updated_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
            new_status = updated_profile.get("relationship_status", "unknown")
            
            await ctx.send(f"aight. set {member.mention}'s score to **{score}**. they are now considered **'{new_status}'**.")
        else:
            await ctx.send("failed to save the score. my brain's broken.")

    @commands.command(name='clear_memories')
    @commands.is_owner()
    async def clear_memories_command(self, ctx):
        if not ctx.guild: return await ctx.send("can't clear memories from a private chat, pal.")
        path = constants.get_summaries_collection_path(self.bot.APP_ID, str(ctx.guild.id))
        if await self.bot.firestore_service.delete_docs(path): await ctx.send("aight, it's done. all the old chatter is gone.")
        else: await ctx.send("couldn't clear the memories. maybe they're stuck.")

    @commands.command(name='forgive_all')
    @commands.is_owner()
    async def forgive_all_command(self, ctx):
        """Resets the relationship score of EVERY user in the server to 0."""
        if not ctx.guild: return await ctx.send("Server only, pal.")
        
        await ctx.send("Aight, hold on. I'm wiping the slate clean...")
        async with ctx.typing():
            # 1. Get all user IDs
            user_ids = await self.bot.firestore_service.get_all_user_ids_in_guild(str(ctx.guild.id))
            
            if not user_ids:
                return await ctx.send("I don't know anyone here yet. Job done.")

            count = 0
            # 2. Reset them one by one
            for user_id in user_ids:
                # Reset score to 0
                await self.bot.firestore_service.save_user_profile_fact(user_id, str(ctx.guild.id), "relationship_score", 0)
                # Reset status to 'neutral'
                await self.bot.firestore_service.save_user_profile_fact(user_id, str(ctx.guild.id), "relationship_status", "neutral")
                count += 1
                
        await ctx.send(f"Done. I forgave {count} people. You're all 'neutral' to me now. Don't make me regret it.")

    @commands.command(name='vibe')
    async def vibe_command(self, ctx, member: discord.Member = None):
        """
        Checks Vinny's opinion of you using the Centralized Tier System.
        """
        target_user = member or ctx.author
        user_id = str(target_user.id)
        guild_id = str(ctx.guild.id) if ctx.guild else None
        
        # 1. Get Profile Data
        profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        if not profile:
            if target_user == ctx.author:
                return await ctx.send("i don't even know who you are yet.")
            else:
                return await ctx.send(f"i don't know who {target_user.display_name} is. never met 'em.")

        rel_score = profile.get("relationship_score", 0)
        
        # 2. USE CENTRALIZED STATUS LOGIC
        rel_status, embed_color = constants.get_relationship_status(rel_score)

        # 3. Get Mood
        mood = self.bot.current_mood
        
        # 4. Generate Comment
        prompt = (
            f"# TASK:\n"
            f"The user '{ctx.author.display_name}' is checking your opinion of '{target_user.display_name}'.\n"
            f"- Your Current Mood: {mood}\n"
            f"- Your Opinion of {target_user.display_name}: {rel_status.upper()} (Score: {rel_score:.1f})\n\n"
            f"Write a short, one-sentence response telling {ctx.author.display_name} exactly what you think of {target_user.display_name}."
        )
        
        async with ctx.typing():
            try:
                response = await self.bot.make_tracked_api_call(
                    model=self.bot.MODEL_NAME, 
                    contents=[prompt], 
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                comment = response.text.strip() if response else "i don't even know what i'm feelin right now."
            except:
                comment = "my brain's fried."

        # 5. Send Embed
        embed = discord.Embed(title=f"Vibe Check: {target_user.display_name}", color=embed_color)
        embed.add_field(name="🧠 Vinny's Mood", value=mood.title(), inline=True)
        embed.add_field(name="❤️ Relationship", value=f"{rel_status.title()} ({rel_score:.0f})", inline=True)
        embed.add_field(name="💬 Vinny says:", value=comment, inline=False)
        
        await ctx.send(embed=embed)

# --- VINNY IMAGE COST TRACKER COMMAND ---
    
    @commands.command(name="vinnycost", hidden=True)
    @commands.is_owner()
    async def vinny_cost(self, ctx):
        """Checks the Daily, Weekly, Monthly, and Total cost of API usage."""
        
        # Fetch detailed stats from Firestore
        stats = await self.bot.firestore_service.get_cost_summary()
        
        if not stats:
            await ctx.send("📉 No ledger data found!")
            return

        daily = stats.get("daily", {})
        weekly = stats.get("weekly", {})
        monthly = stats.get("monthly", {})
        total = stats.get("total", {})
        meta = stats.get("meta", {})

        embed = discord.Embed(title="📉 Vinny's Fiscal Ledger", color=discord.Color.gold())
        embed.description = f"**Data as of:** {meta.get('date', 'Unknown')}"

        def fmt_block(data, label):
            cost = data.get('estimated_cost', 0.0)
            imgs = data.get('images', 0)
            chat = data.get('text_requests', 0)
            return f"**${cost:.4f}**\n({imgs} Imgs | {chat} Chats)"

        embed.add_field(name="📅 Today", value=fmt_block(daily, "Today"), inline=True)
        embed.add_field(name="🗓️ This Week", value=fmt_block(weekly, "Week"), inline=True)
        embed.add_field(name="📆 This Month", value=fmt_block(monthly, "Month"), inline=True)
        embed.add_field(name="💰 All-Time", value=fmt_block(total, "Total"), inline=True)

        await ctx.send(embed=embed)

# --- LEADERBOARD COMMAND ---

    @commands.command(name='leaderboard', aliases=['ranks', 'top', 'boards'])
    async def leaderboard_command(self, ctx):
        """Shows various server leaderboards (Vibe, Earaches, etc.) with pagination."""
        if not ctx.guild: return await ctx.send("Server only, pal.")
        
        async with ctx.typing():
            # --- 1. FETCH DATA FOR ALL BOARDS ---
            top_users, bottom_users = await self.bot.firestore_service.get_leaderboard_data(str(ctx.guild.id))
            yap_users = await self.bot.firestore_service.get_message_leaderboard(str(ctx.guild.id), limit=10)
            
            embeds = []
            
            # --- Helper to format lines ---
            async def format_list(users, emoji_first, emoji_others, value_key='score'):
                text_lines = []
                for i, user in enumerate(users, 1):
                    try:
                        member = ctx.guild.get_member(int(user['id'])) or await ctx.guild.fetch_member(int(user['id']))
                        name = member.display_name
                    except:
                        name = "Unknown Ghost"
                    
                    val = int(user[value_key])
                    
                    # Custom Emoji Logic for the Chat Board
                    if value_key == 'count':
                        if i == 1:
                            emoji = "👑"
                        elif i == 2:
                            emoji = "🥈"
                        elif i == 3:
                            emoji = "🥉"
                        else:
                            emoji = "💬"
                    # Standard logic for the Vibe Board
                    else:
                        emoji = emoji_first if i == 1 else emoji_others
                    
                    # Text Formatting
                    if value_key == 'count': 
                        text_lines.append(f"{emoji} **{i}. {name}**: {val:,} messages")
                    else: 
                        text_lines.append(f"{emoji} **{i}. {name}**: {val}")
                        
                return "\n".join(text_lines)

            # --- 2. BUILD PAGE 1: VIBE BOARD ---
            vibe_embed = discord.Embed(title="🏆 Vinny's Vibe List", description="Here's who I like... and who's on thin ice.", color=discord.Color.gold())
            has_vibe_data = False
            
            if top_users:
                top_text = await format_list(top_users, "👑", "⭐", 'score')
                vibe_embed.add_field(name="💖 Most Loved (The Favorites)", value=top_text, inline=False)
                has_vibe_data = True
            
            if bottom_users:
                negative_users = [u for u in bottom_users if u['score'] < 0]
                if negative_users:
                    bottom_text = await format_list(negative_users, "💀", "💢", 'score')
                    vibe_embed.add_field(name="💔 Most Hated (The Hit List)", value=bottom_text, inline=False)
                    has_vibe_data = True
                    
            if not has_vibe_data:
                vibe_embed.description = "I don't know anyone here well enough yet."
                
            vibe_embed.set_footer(text="Page 1/2 | The Vibe Board")
            embeds.append(vibe_embed)

            # --- 3. BUILD PAGE 2: THE EARACHES ---
            yap_embed = discord.Embed(title="🗣️ The Earaches", description="My ears are bleeding. Here's why.", color=discord.Color.blue())
            if yap_users:
                # The fallback emojis here don't matter because of the custom logic above, but we pass them anyway
                yap_text = await format_list(yap_users, "👑", "💬", 'count') 
                
                # --- THE FIX: Add a field instead of overwriting the description ---
                yap_embed.add_field(name="Top Yappers", value=yap_text, inline=False)
            else:
                yap_embed.description = "Nobody's said a word yet, or I haven't synced the history.\n*(Admins can run `!sync_messages`)*"
                
            yap_embed.set_footer(text="Page 2/2 | The Earaches")
            embeds.append(yap_embed)

            # --- 4. CREATE PAGINATION VIEW ---
            class LeaderboardView(discord.ui.View):
                def __init__(self, embeds):
                    super().__init__(timeout=120) # Buttons expire after 2 minutes
                    self.embeds = embeds
                    self.current_page = 0
                    
                @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.blurple, disabled=True)
                async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if interaction.user != ctx.author:
                        return await interaction.response.send_message("Hey, keep your hands off the remote. You didn't ask for this menu.", ephemeral=True)
                    self.current_page -= 1
                    await self.update_message(interaction)
                    
                @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.blurple)
                async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if interaction.user != ctx.author:
                        return await interaction.response.send_message("Hey, keep your hands off the remote. You didn't ask for this menu.", ephemeral=True)
                    self.current_page += 1
                    await self.update_message(interaction)
                    
                async def update_message(self, interaction: discord.Interaction):
                    # Toggle button accessibility based on the page
                    self.children[0].disabled = self.current_page == 0
                    self.children[1].disabled = self.current_page >= len(self.embeds) - 1
                    await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

            # --- 5. SEND IT ---
            view = LeaderboardView(embeds)
            await ctx.send(embed=embeds[0], view=view)

# --- SYNC MESSAGES COMMAND ---

    @commands.command(name='sync_messages')
    @commands.has_permissions(manage_guild=True)
    async def sync_messages_command(self, ctx):
        """
        [ADMIN ONLY] Reads the entire history of all channels and threads to backfill The Earaches.
        """
        await ctx.send("aight, I'm gonna start reading the ancient scrolls. this might take a *long* time. i'll let you know when I'm done.")
        
        counts = {}
        processed_channels = 0

        # Combine Text Channels, Voice Channels (they have text chats now), and Threads
        all_channels = ctx.guild.text_channels + ctx.guild.voice_channels + list(ctx.guild.threads)

        async with ctx.typing():
            for channel in all_channels:
                # Some channel types (like voice) might not have this permission explicitly checkable the same way, 
                # but we'll try/except to be safe.
                if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                    if not channel.permissions_for(ctx.guild.me).read_message_history:
                        continue
                        
                try:
                    # limit=None tells Discord to fetch literally everything
                    async for msg in channel.history(limit=None):
                        if msg.author.bot: # THIS KEEPS BOTS OFF THE LEADERBOARD
                            continue
                        
                        uid = str(msg.author.id)
                        counts[uid] = counts.get(uid, 0) + 1
                        
                    processed_channels += 1
                except discord.Forbidden:
                    continue # Skip channels Vinny isn't allowed to see
                except AttributeError:
                    continue # Skip if the channel type doesn't support history
                except Exception as e:
                    logging.error(f"Error reading channel {channel.name}: {e}")
                    
            # Save the tallied counts to Firestore
            path = constants.get_user_profile_collection_path(self.bot.APP_ID, str(ctx.guild.id))
            
            # Use a batch to upload them efficiently (Firestore batches support up to 500 operations)
            batch = self.bot.firestore_service.db.batch()
            operations = 0
            
            for uid, count in counts.items():
                doc_ref = self.bot.firestore_service.db.collection(path).document(uid)
                batch.set(doc_ref, {"message_count": count}, merge=True)
                operations += 1
                
                # If we have a massive server with >500 users, commit and start a new batch
                if operations % 450 == 0:
                    await self.bot.loop.run_in_executor(None, batch.commit)
                    batch = self.bot.firestore_service.db.batch()
                    
            # Commit any remaining in the final batch
            if operations % 450 != 0:
                await self.bot.loop.run_in_executor(None, batch.commit)
            
        await ctx.send(f"phew. done reading. i scanned {processed_channels} channels and threads. `!leaderboard` is officially synced.")

# --- ROLE MANAGEMENT COMMANDS ---
   
## ADMIN SETUP COMMAND FOR ROLE COLOR CONFIGURATION ###

    @commands.command(name='setup_rolecolor')
    @commands.has_permissions(manage_guild=True) # CHANGED: Now requires "Manage Server"
    async def setup_rolecolor_command(self, ctx, channel: discord.TextChannel, anchor_role: discord.Role):
        """
        [Admin] Configures the !rolecolor command.
        Usage: !setup_rolecolor #channel @RoleName
        Vinny will only allow color changes in #channel and will place new roles BELOW @RoleName.
        """
        # Save the config as a "fact" for the Guild ID
        config_data = {
            "allowed_channel_id": str(channel.id),
            "anchor_role_id": str(anchor_role.id)
        }
        
        # We save this to the "profile" of the Guild itself so it persists
        success = await self.bot.firestore_service.save_user_profile_fact(
            str(ctx.guild.id), None, "role_config", json.dumps(config_data)
        )
        
        if success:
            await ctx.send(f"got it. i'll only let people change colors in {channel.mention}.\n"
                           f"and any new roles i make will be placed directly below **{anchor_role.name}**.")
        else:
            await ctx.send("my brain's broken. couldn't save the settings.")
    
    @commands.command(name='vinnyversion')
    async def vinnyversion_command(self, ctx):
        """Checks the actual installed version of discord.py"""
        import discord
        await ctx.send(f"I am running on **discord.py version: {discord.__version__}**.")
        
    # --- UPDATED ROLE COMMANDS ---

    @commands.command(name='rolecolor')
    async def rolecolor_command(self, ctx, color1: str, color2: str = None):
        """
        Sets a custom role color.
        Logic adapted strictly from HueTweaker: uses discord.Color objects for both fields.
        """
        if not ctx.guild: return await ctx.send("server only, pal.")

        # 1. PERMISSIONS & CONFIG
        if not ctx.guild.me.guild_permissions.manage_roles:
            return await ctx.send("i need 'Manage Roles' permission first.")

        # Check Channel Config
        server_profile = await self.bot.firestore_service.get_user_profile(str(ctx.guild.id), None)
        role_config = {}
        if server_profile and "role_config" in server_profile:
            try: role_config = json.loads(server_profile["role_config"])
            except: pass
            
        allowed_channel_id = role_config.get("allowed_channel_id")
        if allowed_channel_id and str(ctx.channel.id) != allowed_channel_id:
            return await ctx.send(f"hey! take this over to <#{allowed_channel_id}>.")

        # 2. PARSING 
        def clean_hex(hex_str):
            if not hex_str: return None
            # Regex to keep only valid hex characters
            clean = re.sub(r"[^a-fA-F0-9]", "", hex_str)
            # Expand shorthand (e.g. FFF -> FFFFFF)
            if len(clean) == 3: clean = "".join([c*2 for c in clean])
            return clean

        hex1_str = clean_hex(color1)
        hex2_str = clean_hex(color2)

        if not hex1_str: return await ctx.send(f"'{color1}' ain't a valid hex code.")

        # 3. THE "BLACK FIX" 
        # 000000 is transparent in Discord. Map to 000001.
        if hex1_str == "000000": hex1_str = "000001"
        if hex2_str == "000000": hex2_str = "000001"

        # 4. CONVERT TO DISCORD.COLOR OBJECTS (Crucial Step from cogs/set.py)
       
        val1 = int(hex1_str, 16)
        color_obj_1 = discord.Color(val1)
        
        color_obj_2 = None
        if hex2_str:
            val2 = int(hex2_str, 16)
            color_obj_2 = discord.Color(val2)

        # 5. FIND OR CREATE ROLE
        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)
        
        profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        role = None
        
        # Try finding by ID first
        if profile and "custom_role_id" in profile:
            role = ctx.guild.get_role(int(profile["custom_role_id"]))
        # Fallback to Name
        if not role:
            role = discord.utils.get(ctx.guild.roles, name=ctx.author.name)

        async with ctx.typing():
            try:
                # --- CREATE (If missing) ---
                if not role:
                    role = await ctx.guild.create_role(
                        name=ctx.author.name,
                        color=color_obj_1,
                        reason="Vinny Custom Role"
                    )
                    
                    # Save ID
                    await self.bot.firestore_service.save_user_profile_fact(user_id, guild_id, "custom_role_id", str(role.id))
                    await ctx.send(f"created role: **{role.name}**")

                    # Anchor Position Logic
                    anchor_id = role_config.get("anchor_role_id")
                    if anchor_id:
                        anchor = ctx.guild.get_role(int(anchor_id))
                        if anchor and ctx.guild.me.top_role > anchor:
                            await role.edit(position=max(1, anchor.position - 1))

                # Ensure User has Role
                if role not in ctx.author.roles:
                    await ctx.author.add_roles(role)

                # UPDATE EXISTING ROLE
                await role.edit(
                    color=color_obj_1,
                    reason="Vinny Color Update"
                )

                # --- CONFIRMATION ---
                c1_disp = f"#{hex1_str}"
                # Still output the gradient text to the user if they asked for it, 
                # even though Discord only applies the first color to the actual role.
                c2_disp = f"#{hex2_str}" if hex2_str else None

                if color_obj_2:
                    await ctx.send(f"i can't do actual gradients in discord, pal, but i set **{role.name}** to **{c1_disp}**.")
                else:
                    await ctx.send(f"set **{role.name}** to **{c1_disp}**.")

            except discord.Forbidden:
                await ctx.send("i can't edit that role. is it higher than mine?")
            except Exception as e:
                logging.error(f"Role Error: {e}", exc_info=True)
                await ctx.send(f"something broke: {e}")

    @commands.command(name='rolename')
    async def rolename_command(self, ctx, *, new_name: str):
        """
        Renames your custom color role.
        Usage: !rolename Poopy Butt
        """
        if not ctx.guild: return await ctx.send("server only, pal.")
        
        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)
        
        # 1. FIND THE ROLE
        profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        role = None
        
        # Try ID
        if profile and "custom_role_id" in profile:
            role = ctx.guild.get_role(int(profile["custom_role_id"]))
        
        # Fallback to Name
        if not role:
            role = discord.utils.get(ctx.guild.roles, name=ctx.author.name)

        if not role:
            return await ctx.send("you don't have a custom role yet. use `!rolecolor` first.")

        # 2. RENAME
        old_name = role.name
        try:
            await role.edit(name=new_name, reason=f"Vinny Rename by {ctx.author.name}")
            
            # Save ID just in case it wasn't saved before
            await self.bot.firestore_service.save_user_profile_fact(user_id, guild_id, "custom_role_id", str(role.id))
            
            await ctx.send(f"changed your role from **{old_name}** to **{new_name}**. fancy.")
        except discord.Forbidden:
            await ctx.send("i can't rename that role. permissions issue?")
        except Exception:
            await ctx.send("something broke. couldn't rename it.")
            
async def setup(bot):
    await bot.add_cog(VinnyLogic(bot))