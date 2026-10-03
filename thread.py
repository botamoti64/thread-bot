import asyncio
import logging
import os
from collections import defaultdict
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from common import is_bot_admin

logger = logging.getLogger(__name__)

admin_group = app_commands.Group(name="admin", description="【管理者】BOTの管理権限を設定します")

@admin_group.command(name="add", description="管理権限を持つユーザーまたはロールを追加します")
@app_commands.describe(
    user="管理権限を与えるメンバー",
    role="管理権限を与えるロール"
)
@app_commands.guild_only()
async def admin_add(
    interaction: discord.Interaction,
    user: discord.Member = None,
    role: discord.Role = None
):
    is_server_admin = interaction.guild and interaction.user.guild_permissions.administrator
    is_owner = await interaction.client.is_owner(interaction.user)
    if not (is_server_admin or is_owner):
        await interaction.response.send_message('サーバー管理者またはBOTの所有者のみ実行できます。', ephemeral=True)
        return

    if not user and not role:
        await interaction.response.send_message('ユーザーまたはロールを指定してください。', ephemeral=True)
        return

    if user:
        await interaction.client.db.execute(
            'INSERT OR IGNORE INTO bot_admins (guild_id, target_id, target_type) VALUES (?, ?, ?)',
            (interaction.guild.id, user.id, 'user')
        )
    if role:
        await interaction.client.db.execute(
            'INSERT OR IGNORE INTO bot_admins (guild_id, target_id, target_type) VALUES (?, ?, ?)',
            (interaction.guild.id, role.id, 'role')
        )
    await interaction.client.db.commit()

    msg = []
    if user:
        msg.append(f"ユーザー: {user.mention}")
    if role:
        msg.append(f"ロール: {role.name}")
    await interaction.response.send_message(f"管理権限を追加しました: {', '.join(msg)}", ephemeral=True)

@admin_group.command(name="remove", description="管理権限を削除します")
@app_commands.describe(
    user="管理権限を削除するメンバー",
    role="管理権限を削除するロール"
)
@app_commands.guild_only()
async def admin_remove(
    interaction: discord.Interaction,
    user: discord.Member = None,
    role: discord.Role = None
):
    is_server_admin = interaction.guild and interaction.user.guild_permissions.administrator
    is_owner = await interaction.client.is_owner(interaction.user)
    if not (is_server_admin or is_owner):
        await interaction.response.send_message('サーバー管理者またはBOTの所有者のみ実行できます。', ephemeral=True)
        return

    if not user and not role:
        await interaction.response.send_message('ユーザーまたはロールを指定してください。', ephemeral=True)
        return

    if user:
        await interaction.client.db.execute(
            'DELETE FROM bot_admins WHERE guild_id = ? AND target_id = ? AND target_type = ?',
            (interaction.guild.id, user.id, 'user')
        )
    if role:
        await interaction.client.db.execute(
            'DELETE FROM bot_admins WHERE guild_id = ? AND target_id = ? AND target_type = ?',
            (interaction.guild.id, role.id, 'role')
        )
    await interaction.client.db.commit()

    msg = []
    if user:
        msg.append(f"ユーザー: {user.mention}")
    if role:
        msg.append(f"ロール: {role.name}")
    await interaction.response.send_message(f"管理権限を削除しました: {', '.join(msg)}", ephemeral=True)

@admin_group.command(name="list", description="管理権限を持つユーザーとロールの一覧を表示します")
@app_commands.guild_only()
async def admin_list(interaction: discord.Interaction):
    if not await is_bot_admin(interaction):
        await interaction.response.send_message('権限がありません。', ephemeral=True)
        return

    if interaction.guild is None:
        await interaction.response.send_message('このコマンドはサーバー内でのみ実行できます。', ephemeral=True)
        return

    async with interaction.client.db.execute('SELECT target_id, target_type FROM bot_admins WHERE guild_id = ?', (interaction.guild.id,)) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message('登録されている管理ユーザー・ロールはありません。', ephemeral=True)
        return

    users = []
    roles = []
    for target_id, target_type in rows:
        if target_type == 'user':
            member = interaction.guild.get_member(target_id)
            mention = member.mention if member else f"不明なユーザー(ID: {target_id})"
            users.append(mention)
        elif target_type == 'role':
            role = interaction.guild.get_role(target_id)
            name = role.mention if role else f"不明なロール(ID: {target_id})"
            roles.append(name)

    # 登録件数が多い場合も、Discordの埋め込み上限内で一覧を表示します。
    embeds = []
    embed = discord.Embed(title="BOT管理者一覧", color=discord.Color.blue())
    for label, entries in (("ユーザー", users), ("ロール", roles)):
        chunk = []
        length = 0
        for entry in entries:
            if chunk and length + len(entry) + 1 > 1024:
                embed.add_field(name=label, value="\n".join(chunk), inline=False)
                chunk = []
                length = 0
                if len(embed.fields) >= 4:
                    embeds.append(embed)
                    embed = discord.Embed(title="BOT管理者一覧（続き）", color=discord.Color.blue())
            chunk.append(entry)
            length += len(entry) + 1
        if chunk:
            embed.add_field(name=label, value="\n".join(chunk), inline=False)
    if embed.fields:
        embeds.append(embed)

    await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=True)


class Threadbot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix='/', intents=intents)
        self.db = None
        self.panel_locks = defaultdict(asyncio.Lock)
        self.updating_thread_names = set()
        self._panel_update_tasks = {}
        self._panel_repost_required = set()
        self._closing = False

    def schedule_panel_update(self, channel: discord.TextChannel, repost: bool = False):
        if self._closing:
            return
        if repost:
            self._panel_repost_required.add(channel.id)
        previous_task = self._panel_update_tasks.get(channel.id)
        if previous_task is not None:
            previous_task.cancel()
        self._panel_update_tasks[channel.id] = asyncio.create_task(
            self._delayed_panel_update(channel)
        )

    async def _delayed_panel_update(self, channel: discord.TextChannel):
        try:
            # 連続したイベントをまとめ、同じパネルへの過剰なAPI呼び出しを避けます。
            await asyncio.sleep(2.0)
            if channel.id in self._panel_repost_required:
                self._panel_repost_required.discard(channel.id)
                await repost_panel(self, channel)
            else:
                await update_panel(self, channel)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception('スレッド一覧の更新に失敗しました。')
        finally:
            if self._panel_update_tasks.get(channel.id) is asyncio.current_task():
                self._panel_update_tasks.pop(channel.id, None)

    async def setup_hook(self):
        try:
            self.db = await aiosqlite.connect(Path(__file__).with_name('threads.db'))
            # 公開版は独立したデータベースを使用するため、初回から全列を作成します。
            await self.db.executescript('''
                CREATE TABLE IF NOT EXISTS thread_panels (
                    channel_id INTEGER PRIMARY KEY,
                    panel_message_id INTEGER,
                    guild_id INTEGER,
                    archive_duration INTEGER DEFAULT 1440,
                    is_paused INTEGER DEFAULT 0,
                    max_display_threads INTEGER DEFAULT -1,
                    log_channel_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS thread_creators (
                    thread_id INTEGER PRIMARY KEY,
                    creator_id INTEGER,
                    notification_message_id INTEGER,
                    is_archived INTEGER DEFAULT 0,
                    is_manually_closed INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS thread_extensions (
                    thread_id INTEGER PRIMARY KEY,
                    last_extended_at TEXT,
                    last_real_active_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bot_admins (
                    guild_id INTEGER,
                    target_id INTEGER,
                    target_type TEXT,
                    PRIMARY KEY (guild_id, target_id)
                );
            ''')
            await self.db.commit()
            await self.load_extension('thread_cog')

            from thread_cog import AdminThreadPanelView, ThreadFollowView, ThreadPanelView

            self.add_view(ThreadPanelView())
            self.add_view(AdminThreadPanelView())
            self.add_view(ThreadFollowView())
            self.tree.add_command(admin_group)
            await self.tree.sync()
            self.check_and_update_panels.start()
        except Exception:
            await self.close()
            raise

    @tasks.loop(minutes=1)
    async def check_and_update_panels(self):
        try:
            async with self.db.execute(
                'SELECT channel_id, archive_duration FROM thread_panels'
            ) as cursor:
                rows = await cursor.fetchall()
        except aiosqlite.Error:
            logger.exception('管理チャンネルの取得に失敗しました。')
            return
        cog = self.get_cog('ThreadCog')
        for channel_id, archive_duration in rows:
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    if cog is not None:
                        await cog.close_expired_threads(channel, archive_duration)
                    self.schedule_panel_update(channel)
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                logger.exception('スレッドの期限確認に失敗しました。')

    @check_and_update_panels.before_loop
    async def before_panel_check(self):
        await self.wait_until_ready()

    async def on_ready(self):
        logger.info('スレッドBOTが起動しました。')
        async with self.db.execute('SELECT channel_id FROM thread_panels') as cursor:
            rows = await cursor.fetchall()
        for (channel_id,) in rows:
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    self.schedule_panel_update(channel)
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                logger.exception('起動時のスレッド一覧更新に失敗しました。')

    async def close(self):
        # 更新処理がデータベースを使い終わってから接続を閉じます。
        self._closing = True
        self.check_and_update_panels.cancel()
        pending_tasks = list(self._panel_update_tasks.values())
        check_task = self.check_and_update_panels.get_task()
        if check_task is not None:
            pending_tasks.append(check_task)
        pending_tasks = [task for task in pending_tasks if task is not asyncio.current_task()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._panel_update_tasks.clear()
        self._panel_repost_required.clear()
        try:
            if 'thread_cog' in self.extensions:
                await self.unload_extension('thread_cog')
        finally:
            try:
                if self.db is not None:
                    await self.db.close()
                    self.db = None
            finally:
                await super().close()


bot = Threadbot()


async def update_panel(bot, channel: discord.TextChannel):
    async with bot.panel_locks[channel.id]:
        cog = bot.get_cog('ThreadCog')
        if cog is not None:
            await cog.update_panel_unlocked(channel)


async def repost_panel(
    bot,
    channel: discord.TextChannel,
    archive_duration: int = None,
    max_display_threads: int = None,
    log_channel_id: int = None,
):
    async with bot.panel_locks[channel.id]:
        cog = bot.get_cog('ThreadCog')
        if cog is not None:
            await cog.repost_panel_unlocked(
                channel,
                archive_duration=archive_duration,
                max_display_threads=max_display_threads,
                log_channel_id=log_channel_id,
            )


@bot.tree.command(name='setup', description='【管理者】スレッド管理チャンネルを設定します')
@app_commands.describe(
    days='最後の活動から自動で閉じるまでの日数（1〜365日、-1で自動クローズなし）',
    max_threads='一覧に表示する最大スレッド数（-1で上限なし）',
    log_channel='スレッドの操作ログを送信するチャンネル（省略時は設定しません）',
)
@app_commands.guild_only()
async def setup_thread_channel(
    interaction: discord.Interaction,
    days: int = 1,
    max_threads: int = -1,
    log_channel: discord.TextChannel = None,
):
    if not await is_bot_admin(interaction):
        await interaction.response.send_message('このコマンドを実行する権限がありません。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send('サーバーのテキストチャンネルで実行してください。', ephemeral=True)
        return
    if days != -1 and not 1 <= days <= 365:
        await interaction.followup.send('日数は1〜365、または-1を指定してください。', ephemeral=True)
        return
    if max_threads != -1 and max_threads <= 0:
        await interaction.followup.send('表示件数は1以上、または-1を指定してください。', ephemeral=True)
        return
    archive_duration = -1 if days == -1 else days * 1440
    await repost_panel(
        interaction.client,
        channel,
        archive_duration=archive_duration,
        max_display_threads=max_threads,
        log_channel_id=log_channel.id if log_channel else None,
    )
    await interaction.followup.send(f'{channel.mention}をスレッド管理チャンネルに設定しました。', ephemeral=True)


@bot.tree.command(name='update', description='【管理者】スレッド一覧のパネルを再投稿します')
@app_commands.describe(channel='更新する管理チャンネル（省略時は現在のチャンネル）')
@app_commands.guild_only()
async def update_thread_panel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await is_bot_admin(interaction):
        await interaction.response.send_message('このコマンドを実行する権限がありません。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send('テキストチャンネルを指定してください。', ephemeral=True)
        return
    async with interaction.client.db.execute(
        'SELECT 1 FROM thread_panels WHERE channel_id = ?', (target_channel.id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        await interaction.followup.send(f'{target_channel.mention}は管理チャンネルに登録されていません。', ephemeral=True)
        return
    await repost_panel(interaction.client, target_channel)
    await interaction.followup.send(f'{target_channel.mention}のスレッド一覧を再投稿しました。', ephemeral=True)


@bot.tree.command(name='config', description='【管理者】スレッド管理の設定を変更します')
@app_commands.describe(
    channel='設定を変更するスレッド管理チャンネル（省略時は現在のチャンネル）',
    days='スレッドの自動クローズ日数（1〜365日、または-1で閉じない）',
    pause='スレッド新規作成の一時停止設定（稼働中/一時停止（管理者専用ボタン）/一時停止（ボタン非表示））',
    max_threads='パネルに表示する最大スレッド数（-1で無制限）',
    log_channel='スレッド作成ログを送信するチャンネル',
    remove_log_channel='有効にするとログ送信先の設定を解除します',
    delete_panel='有効にすると管理登録を解除し、このチャンネルの開いているスレッドも閉じます'
)
@app_commands.choices(pause=[
    app_commands.Choice(name="稼働中(スレッド作成ボタンあり)", value=0),
    app_commands.Choice(name="一時停止(管理者専用ボタンあり)", value=1),
    app_commands.Choice(name="一時停止(ボタン非表示)", value=2)
])
@app_commands.guild_only()
async def configure_thread_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    days: int = None,
    pause: int = None,
    max_threads: int = None,
    log_channel: discord.TextChannel = None,
    remove_log_channel: bool = None,
    delete_panel: bool = None
):
    if not await is_bot_admin(interaction):
        await interaction.response.send_message('このコマンドを実行する権限がありません。', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    target_channel = channel or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send('テキストチャンネルを指定してください。', ephemeral=True)
        return

    async with interaction.client.db.execute('SELECT panel_message_id FROM thread_panels WHERE channel_id = ?', (target_channel.id,)) as cursor:
        row = await cursor.fetchone()

    if not row:
        await interaction.followup.send(f'{target_channel.mention}は管理チャンネルとして登録されていません。', ephemeral=True)
        return

    if delete_panel is True:
        # 登録を解除すると、このチャンネルの公開スレッドも閉じます。
        if row and row[0]:
            try:
                panel_msg = await target_channel.fetch_message(row[0])
                await panel_msg.delete()
            except Exception:
                pass

        threads = await target_channel.guild.active_threads()
        active_threads = [t for t in threads if t.parent_id == target_channel.id]

        closed_count = 0
        for thread in active_threads:
            try:
                # 自動復帰を避けるためにフラグを立てて、スレッドをアーカイブ
                await interaction.client.db.execute('INSERT OR REPLACE INTO thread_creators (thread_id, creator_id, is_manually_closed) VALUES (?, ?, 1)', (thread.id, thread.owner_id))
                await interaction.client.db.commit()
                await thread.edit(archived=True, reason="ThreadBOT:パネル削除に伴う一括クローズ")
                closed_count += 1
            except Exception as e:
                logger.exception("パネル削除時にスレッドを閉じられませんでした。")

        thread_ids = [t.id for t in active_threads]

        await interaction.client.db.execute('DELETE FROM thread_panels WHERE channel_id = ?', (target_channel.id,))

        if thread_ids:
            placeholders = ','.join('?' for _ in thread_ids)
            await interaction.client.db.execute(f'DELETE FROM thread_creators WHERE thread_id IN ({placeholders})', thread_ids)
            await interaction.client.db.execute(f'DELETE FROM thread_extensions WHERE thread_id IN ({placeholders})', thread_ids)

        await interaction.client.db.commit()

        await interaction.followup.send(
            f'{target_channel.mention}の管理パネルを削除し、登録を解除しました。\n'
            f'紐づいていた{closed_count}件のスレッドをクローズし、関連データをデータベースから削除しました。',
            ephemeral=True
        )
        return

    updates = {}

    if days is not None:
        if days != -1 and not (1 <= days <= 365):
            await interaction.followup.send('無効な日数です。1〜365、または-1を指定してください。', ephemeral=True)
            return
        updates['archive_duration'] = -1 if days == -1 else days * 1440

    if pause is not None:
        if pause not in (0, 1, 2):
            await interaction.followup.send('一時停止の設定は0、1、2のいずれかを指定してください。', ephemeral=True)
            return
        updates['is_paused'] = pause

    if max_threads is not None:
        if max_threads != -1 and max_threads <= 0:
            await interaction.followup.send('無効な表示スレッド数です。1以上、または-1を指定してください。', ephemeral=True)
            return
        updates['max_display_threads'] = max_threads

    if log_channel is not None:
        updates['log_channel_id'] = log_channel.id
    elif remove_log_channel is True:
        updates['log_channel_id'] = None

    if not updates:
        await interaction.followup.send('変更する設定項目を少なくとも1つ指定してください。', ephemeral=True)
        return

    set_clause = ', '.join([f'{key} = ?' for key in updates.keys()])
    values = list(updates.values())
    values.append(target_channel.id)

    await interaction.client.db.execute(
        f'UPDATE thread_panels SET {set_clause} WHERE channel_id = ?',
        values
    )
    await interaction.client.db.commit()

    await update_panel(interaction.client, target_channel)

    changes = []
    if 'archive_duration' in updates:
        val = '閉じない' if days == -1 else f'{days}日'
        changes.append(f'自動クローズ日数: {val}')
    if 'is_paused' in updates:
        if pause == 0:
            val = '稼働中'
        elif pause == 1:
            val = '一時停止中(管理者専用ボタンあり)'
        elif pause == 2:
            val = '一時停止中(ボタン非表示)'
        else:
            val = '不明'
        changes.append(f'スレッド作成制限: {val}')
    if 'max_display_threads' in updates:
        val = '無制限' if max_threads == -1 else f'{max_threads}件'
        changes.append(f'最大表示スレッド数: {val}')
    if 'log_channel_id' in updates:
        val = '解除（ログ送信なし）' if updates['log_channel_id'] is None else f'<#{updates["log_channel_id"]}>'
        changes.append(f'ログ送信チャンネル: {val}')

    await interaction.followup.send(
        f'{target_channel.mention}の設定を変更しました。\n変更内容:\n' + '\n'.join([f'• {c}' for c in changes]),
        ephemeral=True
    )


@bot.tree.command(name='show_config', description='【管理者】現在のスレッド管理設定を表示します')
@app_commands.describe(channel='設定を確認する管理チャンネル（省略時は現在のチャンネル）')
@app_commands.guild_only()
async def show_config(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await is_bot_admin(interaction):
        await interaction.response.send_message('このコマンドを実行する権限がありません。', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    target_channel = channel or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send('テキストチャンネルを指定してください。', ephemeral=True)
        return

    async with interaction.client.db.execute(
        'SELECT archive_duration, is_paused, max_display_threads, log_channel_id FROM thread_panels WHERE channel_id = ?',
        (target_channel.id,)
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        await interaction.followup.send(f'{target_channel.mention}は管理チャンネルとして登録されていません。', ephemeral=True)
        return

    archive_duration, is_paused, max_display_threads, log_channel_id = row

    if archive_duration == -1:
        days_val = '閉じない(-1)'
    else:
        days_val = f'{archive_duration // 1440}日'

    if is_paused == 0:
        pause_val = '稼働中(0)'
    elif is_paused == 1:
        pause_val = '一時停止中(管理者専用ボタンあり)(1)'
    elif is_paused == 2:
        pause_val = '一時停止中(ボタン非表示)(2)'
    else:
        pause_val = f'不明({is_paused})'

    max_threads_val = '無制限(-1)' if max_display_threads == -1 else f'{max_display_threads}件'
    log_channel_val = f'<#{log_channel_id}>' if log_channel_id else '設定なし'

    embed = discord.Embed(
        title=f"管理設定({target_channel.name})",
        color=discord.Color.blue()
    )
    embed.add_field(name="対象チャンネル", value=target_channel.mention, inline=False)
    embed.add_field(name="自動クローズ設定", value=days_val, inline=True)
    embed.add_field(name="スレッド新規作成制限", value=pause_val, inline=True)
    embed.add_field(name="最大表示スレッド数", value=max_threads_val, inline=True)
    embed.add_field(name="ログ送信チャンネル", value=log_channel_val, inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


if __name__ == '__main__':
    load_dotenv(Path(__file__).with_name('.env'))
    token = os.getenv('TOKEN')
    if not token:
        raise SystemExit('環境変数TOKENにDiscordBOTのトークンを設定してください。')
    logging.basicConfig(level=logging.INFO)
    bot.run(token, log_handler=None)
