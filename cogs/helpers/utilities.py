import re
import logging
import discord

async def check_and_fix_embeds(self, message: discord.Message) -> bool:
        """
        Scans for broken links, fixes them, reposts, and deletes original.
        Uses Strict Regex for x.com to avoid breaking box.com, max.com, etc.
        """
        content = message.content
        fixed_url = None
        
        # 1. Instagram
        if "instagram.com/" in content and "kkinstagram.com" not in content:
            fixed_url = content.replace("instagram.com", "kkinstagram.com")
            
        # 2. TikTok
        elif "tiktok.com/" in content and "kktiktok.com" not in content:
            fixed_url = content.replace("tiktok.com", "kktiktok.com")
            
        # 3. Twitter / X (STRICT REGEX)
        elif ("twitter.com/" in content or "x.com" in content) and "fixupx.com" not in content:
            # Safe replace for twitter.com
            temp_content = content.replace("twitter.com", "fixupx.com")
            
            # Strict replace for x.com (Must be http://x.com or https://www.x.com)
            # It ignores 'box.com', 'linux.com', etc.
            x_pattern = r'(https?://(?:www\.)?)x\.com(?![\w])'
            
            if "x.com" in temp_content:
                fixed_url = re.sub(x_pattern, r'\1fixupx.com', temp_content)
                # If regex didn't change anything (meaning it was a fake match like box.com), cancel fix
                if fixed_url == temp_content:
                    fixed_url = None
            else:
                fixed_url = temp_content

        # --- EXECUTE ---
        if fixed_url and fixed_url != content:
            try:
                await message.channel.send(f"**{message.author.display_name}:**\n{fixed_url}")
                await message.delete()
                return True
            except discord.Forbidden:
                pass # Can't delete? Move on.
            except Exception as e:
                print(f"Embed fix error: {e}")
        
        return False

async def find_user_by_vinny_name(bot_instance, guild: discord.Guild, target_name: str):
    """Finds a user by their nickname stored in Vinny's database."""
    if not bot_instance.firestore_service or not guild: return None
    for member in guild.members:
        nickname = await bot_instance.firestore_service.get_user_nickname(str(member.id))
        if nickname and nickname.lower() == target_name.lower(): return member
    return None