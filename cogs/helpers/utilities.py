import re
import logging
import discord

async def check_and_fix_embeds(message: discord.Message) -> bool:
    """
    Scans for broken links, fixes them immediately, reposts with credit, 
    and deletes the original broken message.
    """
    content = message.content
    fixed_url = None
    
    # --- Identify Potential Fixes (Using the reliable 'kk' domains) ---
    if "instagram.com/" in content and "kkinstagram.com" not in content:
        fixed_url = content.replace("instagram.com", "kkinstagram.com")
    elif "tiktok.com/" in content and "kktiktok.com" not in content:
        fixed_url = content.replace("tiktok.com", "kktiktok.com")
    elif ("twitter.com/" in content or "x.com/" in content) and "fixupx.com" not in content:
        fixed_url = content.replace("twitter.com", "fixupx.com").replace("x.com", "fixupx.com")
    elif "youtube.com/shorts/" in content:
        match = re.search(r"youtube\.com/shorts/([a-zA-Z0-9_-]+)", content)
        if match: fixed_url = f"https://www.youtube.com/watch?v={match.group(1)}"
    elif "music.youtube.com/" in content:
        fixed_url = content.replace("music.youtube.com", "youtube.com")

    # --- The New "Fix & Replace" Logic ---
    if fixed_url:
        await message.channel.send(f"**{message.author.display_name}** posted:\n{fixed_url}")
        try:
            await message.delete()
        except discord.Forbidden:
            logging.warning(f"Could not delete message {message.id} (Missing Permissions).")
        except discord.NotFound:
            pass 
        except Exception as e:
            logging.error(f"Error deleting broken embed message: {e}")
        return True
        
    return False

async def find_user_by_vinny_name(bot_instance, guild: discord.Guild, target_name: str):
    """Finds a user by their nickname stored in Vinny's database."""
    if not bot_instance.firestore_service or not guild: return None
    for member in guild.members:
        nickname = await bot_instance.firestore_service.get_user_nickname(str(member.id))
        if nickname and nickname.lower() == target_name.lower(): return member
    return None