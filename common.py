from datetime import timedelta, timezone

import discord


JST = timezone(timedelta(hours=9), 'JST')


async def is_bot_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if await interaction.client.is_owner(interaction.user):
        return True
    if interaction.user.guild_permissions.administrator:
        return True

    # 登録したユーザー・ロールの管理権限は、登録先のサーバー内だけで有効です。
    role_ids = [role.id for role in interaction.user.roles]
    placeholders = ','.join('?' for _ in role_ids)
    query = f'''
        SELECT 1 FROM bot_admins
        WHERE guild_id = ? AND (
            (target_id = ? AND target_type = 'user') OR
            (target_id IN ({placeholders}) AND target_type = 'role')
        )
    '''
    async with interaction.client.db.execute(
        query, [interaction.guild.id, interaction.user.id, *role_ids]
    ) as cursor:
        return await cursor.fetchone() is not None
