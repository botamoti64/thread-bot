import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from common import JST, is_bot_admin

logger = logging.getLogger(__name__)

def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc(value: str):
    try:
        return _as_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def get_last_active_time(thread: discord.Thread):
    if thread.last_message_id:
        return discord.utils.snowflake_time(thread.last_message_id)
    return thread.created_at or datetime.fromtimestamp(0, tz=timezone.utc)


def get_thread_active_time(thread: discord.Thread, extensions: dict = None):
    last_active = get_last_active_time(thread)
    if extensions and thread.id in extensions:
        extended_at, real_active_at = extensions[thread.id]
        extension = _parse_utc(extended_at)
        real_active = _parse_utc(real_active_at)
        # 延長用の一時投稿は、利用者による最後の活動日時に含めない。
        if extension and last_active <= extension:
            return real_active or extension
    return last_active


async def get_thread_extensions(bot):
    async with bot.db.execute(
        'SELECT thread_id, last_extended_at, last_real_active_at FROM thread_extensions'
    ) as cursor:
        rows = await cursor.fetchall()
    return {thread_id: (extended_at, real_active_at)
            for thread_id, extended_at, real_active_at in rows}


def build_thread_panel(active_threads, extensions, max_display_threads, is_paused):
    description = (
        'このチャンネルのアクティブなスレッド一覧です。\n'
        '作成者は/renameで名前を変更し、/closeで閉じることができます。'
    )
    if is_paused == 1:
        description += '\n\n現在、新規スレッドの作成を一時停止しています。管理者のみ作成できます。'
        view = AdminThreadPanelView()
    elif is_paused == 2:
        description += '\n\n現在、新規スレッドの作成を一時停止しています。'
        view = None
    else:
        description += '\n下のボタンから新しいスレッドを作成できます。'
        view = ThreadPanelView()

    candidates = active_threads if max_display_threads == -1 else active_threads[:max_display_threads]
    lines = []
    for thread in candidates:
        active_at = get_thread_active_time(thread, extensions).astimezone(JST)
        line = f'• {thread.mention} ({active_at:%Y/%m/%d %H:%M})'
        remaining = len(active_threads) - len(lines) - 1
        suffix = f'\n\nほかに{remaining}件のスレッドがあります。' if remaining else ''
        # Discordの説明欄の上限内に収め、残りは全件表示から確認できるようにする。
        if len(description + '\n\n' + '\n'.join(lines + [line]) + suffix) > 4096:
            break
        lines.append(line)
    if lines:
        description += '\n\n' + '\n'.join(lines)
    elif not active_threads:
        description += '\n\n現在、アクティブなスレッドはありません。'
    remaining = len(active_threads) - len(lines)
    if remaining:
        description += f'\n\nほかに{remaining}件のスレッドがあります。'
    elif view:
        for button in list(view.children):
            if button.custom_id.endswith('show_all_button'):
                view.remove_item(button)
    embed = discord.Embed(title='アクティブなスレッド一覧', description=description,
                          color=discord.Color.blurple())
    return embed, view


class ThreadCreateModal(discord.ui.Modal, title='新規スレッドの作成'):
    thread_name = discord.ui.TextInput(
        label='スレッド名',
        placeholder='作成するスレッドの名前を入力してください',
        min_length=1,
        max_length=30,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        name = self.thread_name.value.strip()
        if not name:
            await interaction.followup.send('スレッド名を入力してください。', ephemeral=True)
            return
        async with interaction.client.db.execute(
            'SELECT is_paused, log_channel_id FROM thread_panels WHERE channel_id = ?',
            (channel.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await interaction.followup.send('このチャンネルは管理対象から解除されています。', ephemeral=True)
            return
        is_paused, log_channel_id = row
        # モーダルを開いた後に管理設定が変わる場合も、送信時の設定を適用する。
        if is_paused == 2 or (is_paused == 1 and not await is_bot_admin(interaction)):
            await interaction.followup.send('現在、新規スレッドの作成を一時停止しています。', ephemeral=True)
            return
        try:
            try:
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread,
                    auto_archive_duration=10080, reason='ThreadBOT:パネルからのスレッド作成'
                )
            except discord.HTTPException as error:
                if error.status != 400:
                    raise
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread,
                    auto_archive_duration=1440, reason='ThreadBOT:パネルからのスレッド作成'
                )
        except discord.HTTPException:
            logger.exception('スレッドの作成に失敗しました。')
            await interaction.followup.send('スレッドを作成できませんでした。BOTの権限を確認してください。', ephemeral=True)
            return

        # 通知の送信に失敗しても、作成者がスレッドを管理できるよう先に記録する。
        await interaction.client.db.execute(
            'INSERT INTO thread_creators (thread_id, creator_id) VALUES (?, ?)',
            (thread.id, interaction.user.id)
        )
        await interaction.client.db.commit()
        time_str = datetime.now(JST).strftime('%Y/%m/%d %H:%M')
        try:
            notification = await channel.send(
                f'スレッド{thread.mention}が作成されました。\n'
                f'作成者: {interaction.user.mention}\n作成日時: {time_str}',
                view=ThreadFollowView(), allowed_mentions=discord.AllowedMentions.none()
            )
            await interaction.client.db.execute(
                'UPDATE thread_creators SET notification_message_id = ? WHERE thread_id = ?',
                (notification.id, thread.id)
            )
            await interaction.client.db.commit()
        except discord.HTTPException:
            logger.exception('スレッドの作成通知を送信できませんでした。')
        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            logger.exception('作成者をスレッドに追加できませんでした。')
        if log_channel_id:
            try:
                log_channel = interaction.guild.get_channel(log_channel_id)
                if log_channel is None:
                    log_channel = await interaction.guild.fetch_channel(log_channel_id)
                embed = discord.Embed(title='スレッド作成ログ', color=discord.Color.green(),
                                      timestamp=datetime.now(timezone.utc))
                embed.add_field(name='スレッド', value=f'{thread.mention} ({thread.name})', inline=False)
                embed.add_field(name='作成者', value=interaction.user.mention, inline=False)
                embed.add_field(name='作成日時', value=time_str, inline=False)
                await log_channel.send(embed=embed)
            except discord.HTTPException:
                logger.exception('スレッドの作成ログを送信できませんでした。')
        interaction.client.schedule_panel_update(channel, repost=True)
        await interaction.followup.send(f'スレッド{thread.mention}を作成しました。', ephemeral=True)


class ThreadFollowView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label='スレッドをフォロー',
        style=discord.ButtonStyle.success,
        custom_id='persistent_thread_follow_button'
    )
    async def follow_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with interaction.client.db.execute(
            'SELECT thread_id FROM thread_creators WHERE notification_message_id = ?',
            (interaction.message.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            await interaction.followup.send('対象のスレッドが見つかりませんでした。', ephemeral=True)
            return
        try:
            thread = interaction.guild.get_thread(row[0])
            if thread is None:
                thread = await interaction.guild.fetch_channel(row[0])
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            logger.exception('スレッドのフォローに失敗しました。')
            await interaction.followup.send('スレッドをフォローできませんでした。削除済みか、権限が不足している可能性があります。', ephemeral=True)
            return
        await interaction.followup.send(f'スレッド{thread.mention}をフォローしました。', ephemeral=True)

async def show_all_threads(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message('テキストチャンネルでのみ使用できます。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    threads = await channel.guild.active_threads()
    active_threads = [thread for thread in threads if thread.parent_id == channel.id]
    extensions = await get_thread_extensions(interaction.client)
    active_threads.sort(key=lambda thread: get_thread_active_time(thread, extensions), reverse=True)
    if not active_threads:
        await interaction.followup.send('現在、アクティブなスレッドはありません。', ephemeral=True)
        return
    # 複数のEmbedを一括送信すると合計6000文字の制限に達するため、ページごとに送信する。
    for start in range(0, len(active_threads), 50):
        page = active_threads[start:start + 50]
        lines = []
        for thread in page:
            active_at = get_thread_active_time(thread, extensions).astimezone(JST)
            lines.append(f'• {thread.mention} ({active_at:%Y/%m/%d %H:%M})')
        embed = discord.Embed(
            title=f'全スレッド一覧({start + 1}〜{start + len(page)}件目/全{len(active_threads)}件)',
            description='\n'.join(lines), color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ThreadPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label='スレッド作成',
        style=discord.ButtonStyle.primary,
        custom_id='persistent_thread_create_button'
    )
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with interaction.client.db.execute('SELECT channel_id, is_paused FROM thread_panels WHERE channel_id = ?', (interaction.channel_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message('このチャンネルはスレッド管理用チャンネルとして登録されていません。', ephemeral=True)
            return

        channel_id, is_paused = row
        if is_paused != 0:
            await interaction.response.send_message('このチャンネルでの新規スレッド作成は一時的に停止されています。', ephemeral=True)
            return

        await interaction.response.send_modal(ThreadCreateModal())

    @discord.ui.button(
        label='すべて表示',
        style=discord.ButtonStyle.secondary,
        custom_id='persistent_thread_show_all_button'
    )
    async def show_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_all_threads(interaction)


class AdminThreadPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label='スレッド作成（管理者専用）',
        style=discord.ButtonStyle.danger,
        custom_id='persistent_admin_thread_create_button'
    )
    async def admin_create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_bot_admin(interaction):
            await interaction.response.send_message('このボタンは管理者のみ使用できます。', ephemeral=True)
            return

        await interaction.response.send_modal(ThreadCreateModal())

    @discord.ui.button(
        label='すべて表示',
        style=discord.ButtonStyle.secondary,
        custom_id='persistent_admin_thread_show_all_button'
    )
    async def show_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_all_threads(interaction)


class ThreadCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


    async def update_panel_unlocked(self, channel: discord.TextChannel):
        async with self.bot.db.execute(
            'SELECT panel_message_id, max_display_threads, is_paused FROM thread_panels WHERE channel_id = ?',
            (channel.id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return
        panel_message_id, max_display_threads, is_paused = row
        threads = await channel.guild.active_threads()
        active_threads = [thread for thread in threads if thread.parent_id == channel.id]
        extensions = await get_thread_extensions(self.bot)
        active_threads.sort(key=lambda thread: get_thread_active_time(thread, extensions), reverse=True)
        embed, view = build_thread_panel(active_threads, extensions, max_display_threads, is_paused)
        if panel_message_id:
            try:
                message = await channel.fetch_message(panel_message_id)
                if message.embeds and message.embeds[0].description == embed.description:
                    return
                embed.set_footer(text=f'更新日時: {datetime.now(JST):%Y/%m/%d %H:%M:%S}')
                await message.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.exception('管理パネルを更新できませんでした。')
                return
        await self.repost_panel_unlocked(channel)

    async def repost_panel_unlocked(self, channel: discord.TextChannel,
                                    archive_duration: int = None,
                                    max_display_threads: int = None,
                                    log_channel_id: int = None):
        async with self.bot.db.execute(
            'SELECT panel_message_id, archive_duration, max_display_threads, is_paused, log_channel_id '
            'FROM thread_panels WHERE channel_id = ?', (channel.id,)
        ) as cursor:
            row = await cursor.fetchone()
        old_message_id = row[0] if row else None
        if archive_duration is None:
            archive_duration = row[1] if row else 1440
        if max_display_threads is None:
            max_display_threads = row[2] if row else -1
        is_paused = row[3] if row else 0
        if log_channel_id is None:
            log_channel_id = row[4] if row else None
        threads = await channel.guild.active_threads()
        active_threads = [thread for thread in threads if thread.parent_id == channel.id]
        extensions = await get_thread_extensions(self.bot)
        active_threads.sort(key=lambda thread: get_thread_active_time(thread, extensions), reverse=True)
        embed, view = build_thread_panel(active_threads, extensions, max_display_threads, is_paused)
        embed.set_footer(text=f'更新日時: {datetime.now(JST):%Y/%m/%d %H:%M:%S}')
        # 新しいパネルの送信・登録が成功するまで、既存パネルを保持する。
        new_message = await channel.send(embed=embed, view=view)
        await self.bot.db.execute(
            '''
            INSERT INTO thread_panels
                (channel_id, panel_message_id, guild_id, archive_duration, max_display_threads, log_channel_id, is_paused)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                panel_message_id = excluded.panel_message_id,
                guild_id = excluded.guild_id,
                archive_duration = excluded.archive_duration,
                max_display_threads = excluded.max_display_threads,
                log_channel_id = excluded.log_channel_id,
                is_paused = excluded.is_paused
            ''',
            (channel.id, new_message.id, channel.guild.id, archive_duration,
             max_display_threads, log_channel_id, is_paused)
        )
        await self.bot.db.commit()
        if old_message_id:
            try:
                old_message = await channel.fetch_message(old_message_id)
                await old_message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.exception('以前の管理パネルを削除できませんでした。')

    async def close_thread_notification(self, thread: discord.Thread, parent_channel: discord.TextChannel):
        async with self.bot.db.execute('SELECT notification_message_id, is_archived FROM thread_creators WHERE thread_id = ?', (thread.id,)) as cursor:
            row = await cursor.fetchone()

        notification_message_id = None
        is_archived = 0

        if not row:
            await self.bot.db.execute(
                'INSERT OR IGNORE INTO thread_creators (thread_id, creator_id, is_archived, is_manually_closed) VALUES (?, ?, 1, 0)',
                (thread.id, thread.owner_id)
            )
            await self.bot.db.commit()
        else:
            notification_message_id, is_archived = row

        await self.bot.db.execute('UPDATE thread_creators SET is_archived = 1 WHERE thread_id = ?', (thread.id,))
        await self.bot.db.execute('DELETE FROM thread_extensions WHERE thread_id = ?', (thread.id,))
        await self.bot.db.commit()

        if notification_message_id:
            try:
                msg = await parent_channel.fetch_message(notification_message_id)
                content = msg.content
                if "閉じられました" not in content:
                    new_content = re.sub(r'が(作成|再開)されました。', 'が閉じられました。', content, count=1)
                    await msg.edit(content=new_content, view=None)
            except Exception:
                logger.exception("作成通知メッセージのクローズ更新に失敗しました。")

    async def reopen_thread_notification(self, thread: discord.Thread, parent_channel: discord.TextChannel, operator: discord.Member = None):
        async with self.bot.db.execute('SELECT notification_message_id, is_archived FROM thread_creators WHERE thread_id = ?', (thread.id,)) as cursor:
            row = await cursor.fetchone()

        notification_message_id = None
        is_archived = 1

        if not row:
            await self.bot.db.execute(
                'INSERT OR IGNORE INTO thread_creators (thread_id, creator_id, is_archived, is_manually_closed) VALUES (?, ?, 0, 0)',
                (thread.id, thread.owner_id)
            )
            await self.bot.db.commit()
        else:
            notification_message_id, is_archived = row

        if is_archived == 1:
            await self.bot.db.execute('UPDATE thread_creators SET is_archived = 0, is_manually_closed = 0 WHERE thread_id = ?', (thread.id,))

            now = datetime.now(timezone.utc)
            await self.bot.db.execute(
                'INSERT OR REPLACE INTO thread_extensions (thread_id, last_extended_at, last_real_active_at) VALUES (?, ?, ?)',
                (thread.id, now.isoformat(), now.isoformat())
            )
            await self.bot.db.commit()

            if notification_message_id:
                try:
                    msg = await parent_channel.fetch_message(notification_message_id)
                    content = msg.content
                    if "再開されました" not in content:
                        new_content = re.sub(r'が(?:作成されました|閉じられました)。', 'が再開されました。', content, count=1)
                        await msg.edit(content=new_content, view=ThreadFollowView())
                except Exception:
                    logger.exception("作成通知メッセージの再開更新に失敗しました。")

            async with self.bot.db.execute('SELECT log_channel_id FROM thread_panels WHERE channel_id = ?', (parent_channel.id,)) as cursor:
                p_row = await cursor.fetchone()
            log_channel_id = p_row[0] if p_row else None

            if log_channel_id:
                log_channel = thread.guild.get_channel(log_channel_id)
                if not log_channel:
                    try:
                        log_channel = await thread.guild.fetch_channel(log_channel_id)
                    except Exception:
                        pass
                if log_channel:
                    operator_mention = "不明"
                    if operator:
                        operator_mention = f"{operator.mention} ({operator.name})"
                    else:
                        try:
                            async for entry in thread.guild.audit_logs(limit=5, action=discord.AuditLogAction.thread_update):
                                if entry.target.id == thread.id and getattr(entry.before, 'archived', None) is True and getattr(entry.after, 'archived', None) is False:
                                    user = entry.user
                                    member = thread.guild.get_member(user.id)
                                    if not member:
                                        try:
                                            member = await thread.guild.fetch_member(user.id)
                                        except Exception:
                                            pass
                                    if member:
                                        operator_mention = f"{member.mention} ({member.name})"
                                    else:
                                        operator_mention = f"{user.name} (ID: {user.id})"
                                    break
                        except Exception:
                            logger.exception("再開の監査ログ取得に失敗しました。")

                    try:
                        embed = discord.Embed(
                            title="スレッド再開ログ",
                            color=discord.Color.orange(),
                            timestamp=datetime.now(timezone.utc)
                        )
                        embed.add_field(name="スレッド", value=f"{thread.mention} ({thread.name})", inline=False)
                        embed.add_field(name="実行者", value=operator_mention, inline=False)
                        await log_channel.send(embed=embed)
                    except Exception:
                        logger.exception("再開ログの送信に失敗しました。")

            self.bot.schedule_panel_update(parent_channel)


    async def close_expired_threads(self, channel: discord.TextChannel, archive_duration: int):
        now = datetime.now(timezone.utc)
        threads = await channel.guild.active_threads()
        active_threads = [thread for thread in threads if thread.parent_id == channel.id]
        async with self.bot.db.execute(
            'SELECT log_channel_id FROM thread_panels WHERE channel_id = ?', (channel.id,)
        ) as cursor:
            row = await cursor.fetchone()
        log_channel = None
        if row and row[0]:
            log_channel = channel.guild.get_channel(row[0])
            if log_channel is None:
                try:
                    log_channel = await channel.guild.fetch_channel(row[0])
                except discord.HTTPException:
                    logger.exception('操作ログの送信先を取得できませんでした。')

        for thread in active_threads:
            last_active = _as_utc(get_last_active_time(thread))
            real_active = last_active
            async with self.bot.db.execute(
                'SELECT last_extended_at, last_real_active_at FROM thread_extensions WHERE thread_id = ?',
                (thread.id,)
            ) as cursor:
                extension = await cursor.fetchone()
            if extension:
                extended_at = _parse_utc(extension[0])
                if extended_at is None or last_active > extended_at:
                    await self.bot.db.execute('DELETE FROM thread_extensions WHERE thread_id = ?', (thread.id,))
                    await self.bot.db.commit()
                else:
                    last_active = max(last_active, extended_at)
                    real_active = _parse_utc(extension[1]) or extended_at

            if archive_duration != -1 and (now - real_active).total_seconds() >= archive_duration * 60:
                try:
                    await thread.edit(archived=True, reason='ThreadBOT:設定した終了期限を経過したため')
                except discord.HTTPException:
                    logger.exception('スレッドを自動で閉じられませんでした。')
                    continue
                await self.bot.db.execute('DELETE FROM thread_extensions WHERE thread_id = ?', (thread.id,))
                await self.bot.db.commit()
                if log_channel:
                    try:
                        embed = discord.Embed(title='スレッド自動終了ログ', color=discord.Color.red(),
                                              timestamp=now)
                        embed.add_field(name='スレッド', value=f'{thread.mention} ({thread.name})', inline=False)
                        embed.add_field(name='理由', value=f'最後の投稿または再開から{archive_duration // 1440}日経過したため', inline=False)
                        await log_channel.send(embed=embed)
                    except discord.HTTPException:
                        logger.exception('スレッドの終了ログを送信できませんでした。')
                continue

            native_limit = thread.auto_archive_duration * 60
            # 1時間・24時間設定でも、Discord側の期限に余裕を持たせて延長する。
            extension_interval = native_limit - min(86400, native_limit // 4)
            if (now - last_active).total_seconds() < extension_interval:
                continue
            try:
                temporary_message = await thread.send('スレッドの自動アーカイブを延長しています。')
            except discord.HTTPException:
                logger.exception('スレッドの自動アーカイブを延長できませんでした。')
                continue
            # 延長のための投稿で、BOTに設定した終了期限をリセットしない。
            await self.bot.db.execute(
                'INSERT OR REPLACE INTO thread_extensions (thread_id, last_extended_at, last_real_active_at) VALUES (?, ?, ?)',
                (thread.id, temporary_message.created_at.isoformat(), real_active.isoformat())
            )
            await self.bot.db.commit()
            try:
                await temporary_message.delete()
            except discord.HTTPException:
                logger.exception('自動延長用の一時メッセージを削除できませんでした。')
            if log_channel:
                try:
                    embed = discord.Embed(title='スレッド自動延長ログ', color=discord.Color.blue(), timestamp=now)
                    embed.add_field(name='スレッド', value=f'{thread.mention} ({thread.name})', inline=False)
                    embed.add_field(name='詳細', value='Discord側の自動アーカイブを延長しました。BOTに設定した終了期限は変わりません。', inline=False)
                    await log_channel.send(embed=embed)
                except discord.HTTPException:
                    logger.exception('スレッドの延長ログを送信できませんでした。')

    @app_commands.command(name='close', description='スレッドを閉じます（作成者・管理者用）')
    @app_commands.guild_only()
    async def close_thread(self, interaction: discord.Interaction):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message('このコマンドはスレッド内でのみ使用できます。', ephemeral=True)
            return
        is_admin = await is_bot_admin(interaction)
        async with self.bot.db.execute(
            'SELECT creator_id, is_manually_closed FROM thread_creators WHERE thread_id = ?',
            (thread.id,)
        ) as cursor:
            row = await cursor.fetchone()
        creator_id = row[0] if row else thread.owner_id
        was_manually_closed = row[1] if row else 0
        is_creator = creator_id == interaction.user.id
        if not (is_creator or is_admin):
            await interaction.response.send_message('スレッドの作成者または管理者のみが、このスレッドを閉じられます。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # Gatewayの終了イベントを処理する前に、手動操作であることを記録する。
        await self.bot.db.execute(
            '''
            INSERT INTO thread_creators (thread_id, creator_id, is_manually_closed)
            VALUES (?, ?, 1)
            ON CONFLICT(thread_id) DO UPDATE SET is_manually_closed = 1
            ''', (thread.id, creator_id)
        )
        await self.bot.db.commit()
        try:
            await thread.edit(archived=True, reason='ThreadBOT:作成者または管理者による終了')
        except discord.HTTPException:
            logger.exception('スレッドを閉じられませんでした。')
            await self.bot.db.execute(
                'UPDATE thread_creators SET is_manually_closed = ? WHERE thread_id = ?',
                (was_manually_closed, thread.id)
            )
            await self.bot.db.commit()
            await interaction.followup.send('スレッドを閉じられませんでした。BOTの権限を確認してください。', ephemeral=True)
            return

        async with self.bot.db.execute(
            'SELECT log_channel_id FROM thread_panels WHERE channel_id = ?', (thread.parent_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            try:
                log_channel = thread.guild.get_channel(row[0])
                if log_channel is None:
                    log_channel = await thread.guild.fetch_channel(row[0])
                embed = discord.Embed(title='スレッド終了ログ', color=discord.Color.red(),
                                      timestamp=datetime.now(timezone.utc))
                embed.add_field(name='スレッド', value=f'{thread.mention} ({thread.name})', inline=False)
                operator_type = '作成者' if is_creator else '管理者'
                embed.add_field(name='実行者', value=f'{interaction.user.mention} ({operator_type})', inline=False)
                await log_channel.send(embed=embed)
            except discord.HTTPException:
                logger.exception('スレッドの終了ログを送信できませんでした。')
        # 操作後の通知失敗で手動終了フラグを戻すと、閉じたスレッドが自動再開されてしまう。
        try:
            await interaction.followup.send('スレッドを閉じました。', ephemeral=True)
        except discord.HTTPException:
            logger.exception('スレッドは終了しましたが、結果通知を送信できませんでした。')

    @app_commands.command(name='rename', description='スレッドの名前を変更します（作成者・管理者用）')
    @app_commands.describe(name='新しいスレッド名')
    @app_commands.guild_only()
    async def rename_thread(self, interaction: discord.Interaction, name: str):
        thread = interaction.channel

        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message('このコマンドはスレッド内でのみ実行できます。', ephemeral=True)
            return

        name = name.strip()
        if not name:
            await interaction.response.send_message('スレッド名を入力してください。', ephemeral=True)
            return

        if len(name) > 30:
            await interaction.response.send_message('スレッド名は30文字以内で指定してください。', ephemeral=True)
            return

        async with self.bot.db.execute('SELECT creator_id FROM thread_creators WHERE thread_id = ?', (thread.id,)) as cursor:
            row = await cursor.fetchone()

        creator_id = row[0] if row else thread.owner_id

        is_creator = (creator_id == interaction.user.id)
        is_admin = await is_bot_admin(interaction)

        if not (is_creator or is_admin):
            await interaction.response.send_message('スレッドの作成者または管理者のみがスレッドの名前を変更できます。', ephemeral=True)
            return

        await interaction.response.defer()
        old_name = thread.name
        self.bot.updating_thread_names.add(thread.id)
        try:
            await thread.edit(name=name, reason='ThreadBOT:作成者または管理者による名前変更')
        except discord.HTTPException:
            self.bot.updating_thread_names.discard(thread.id)
            logger.exception('スレッド名を変更できませんでした。')
            await interaction.followup.send('スレッド名を変更できませんでした。BOTの権限を確認してください。', ephemeral=True)
            return
        try:
            async with self.bot.db.execute('SELECT log_channel_id FROM thread_panels WHERE channel_id = ?', (thread.parent_id,)) as cursor:
                p_row = await cursor.fetchone()
            log_channel_id = p_row[0] if p_row else None

            if log_channel_id:
                log_channel = thread.guild.get_channel(log_channel_id)
                if not log_channel:
                    try:
                        log_channel = await thread.guild.fetch_channel(log_channel_id)
                    except Exception:
                        pass
                if log_channel:
                    try:
                        embed = discord.Embed(
                            title="スレッド名変更ログ",
                            color=discord.Color.blue(),
                            timestamp=datetime.now(timezone.utc)
                        )
                        embed.add_field(name="スレッド", value=f"{thread.mention} ({name})", inline=False)
                        embed.add_field(name="変更前", value=old_name, inline=False)
                        embed.add_field(name="変更後", value=name, inline=False)
                        by_str = "管理者" if is_admin and not is_creator else "作成者"
                        embed.add_field(name="実行者", value=f"{interaction.user.mention} ({by_str})", inline=False)
                        await log_channel.send(embed=embed)
                    except Exception:
                        logger.exception("名前変更ログの送信に失敗しました。")
        except discord.HTTPException:
            logger.exception('スレッド名の変更ログを送信できませんでした。')
        try:
            await interaction.followup.send(f'スレッドの名前を「{old_name}」から「{name}」に変更しました。', ephemeral=False)
        except discord.HTTPException:
            logger.exception('スレッド名は変更しましたが、結果通知を送信できませんでした。')

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        async with self.bot.db.execute('SELECT channel_id FROM thread_panels WHERE channel_id = ?', (thread.parent_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return

        async with self.bot.db.execute('SELECT creator_id, is_archived FROM thread_creators WHERE thread_id = ?', (thread.id,)) as cursor:
            creator_row = await cursor.fetchone()

        if creator_row:
            parent_channel = thread.parent
            if not parent_channel:
                parent_channel = thread.guild.get_channel(thread.parent_id)
                if not parent_channel:
                    try:
                        parent_channel = await thread.guild.fetch_channel(thread.parent_id)
                    except discord.HTTPException:
                        pass
            if parent_channel:
                await self.reopen_thread_notification(thread, parent_channel)
            return

        if thread.owner_id != self.bot.user.id:
            channel = thread.parent
            if not channel:
                channel = thread.guild.get_channel(thread.parent_id)
            if not channel:
                try:
                    channel = await thread.guild.fetch_channel(thread.parent_id)
                except discord.HTTPException:
                    pass
            if not channel:
                return

            await self.bot.db.execute(
                'INSERT OR REPLACE INTO thread_creators (thread_id, creator_id) VALUES (?, ?)',
                (thread.id, thread.owner_id)
            )
            await self.bot.db.commit()

            discord_duration = 10080
            try:
                await thread.edit(auto_archive_duration=discord_duration)
            except discord.HTTPException:
                try:
                    await thread.edit(auto_archive_duration=1440)
                except Exception:
                    pass
            except Exception:
                pass

            self.bot.schedule_panel_update(channel, repost=True)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        parent_channel = after.parent
        if not parent_channel:
            parent_channel = after.guild.get_channel(after.parent_id)
        if not parent_channel:
            try:
                parent_channel = await after.guild.fetch_channel(after.parent_id)
            except discord.HTTPException:
                pass

        if not parent_channel:
            return

        async with self.bot.db.execute('SELECT archive_duration, log_channel_id FROM thread_panels WHERE channel_id = ?', (after.parent_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return

        archive_duration, log_channel_id = row

        async with self.bot.db.execute('SELECT is_archived FROM thread_creators WHERE thread_id = ?', (after.id,)) as cursor:
            creator_row = await cursor.fetchone()
        db_is_archived = creator_row[0] if creator_row else None

        state_changed = False
        if before.archived != after.archived:
            state_changed = True
        elif db_is_archived is not None and db_is_archived != (1 if after.archived else 0):
            state_changed = True

        if state_changed:
            if after.archived:
                await self.close_thread_notification(after, parent_channel)

            else:
                await self.reopen_thread_notification(after, parent_channel)

        async with self.bot.db.execute('SELECT is_manually_closed FROM thread_creators WHERE thread_id = ?', (after.id,)) as cursor:
            row = await cursor.fetchone()
        is_manually_closed = row[0] if row else 0
        if is_manually_closed == 1:
            if before.archived != after.archived or before.name != after.name:
                self.bot.schedule_panel_update(parent_channel)
            return

        if not before.archived and after.archived:
            if archive_duration == -1:
                is_admin_action = False
                try:
                    async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.thread_update):
                        if entry.target.id == after.id and getattr(entry.after, 'archived', None) is True:
                            if entry.user.id == self.bot.user.id:
                                break

                            member = after.guild.get_member(entry.user.id)
                            if not member:
                                try:
                                    member = await after.guild.fetch_member(entry.user.id)
                                except discord.HTTPException:
                                    pass

                            if member and member.guild_permissions.administrator:
                                is_admin_action = True
                            break
                except Exception:
                    logger.exception("監査ログの取得に失敗しました。")

                if not is_admin_action:
                    try:
                        await after.edit(archived=False, reason="ThreadBOT:閉じないスレッドのため自動復旧")
                        return
                    except Exception:
                        logger.exception("スレッドの自動アーカイブ解除に失敗しました。")

        if before.name != after.name:
            if after.id in self.bot.updating_thread_names:
                self.bot.updating_thread_names.discard(after.id)
            else:
                log_channel = None
                if log_channel_id:
                    log_channel = after.guild.get_channel(log_channel_id)
                if log_channel_id and not log_channel:
                    try:
                        log_channel = await after.guild.fetch_channel(log_channel_id)
                    except discord.HTTPException:
                        pass
                if log_channel:
                    operator_mention = "不明"
                    should_send_log = True
                    try:
                        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.thread_update):
                            if entry.target.id == after.id:
                                is_name_change = getattr(entry.after, 'name', None) == after.name and getattr(entry.before, 'name', None) == before.name
                                if is_name_change:
                                    user = entry.user
                                    if user.id == self.bot.user.id:
                                        should_send_log = False
                                        break

                                    member = after.guild.get_member(user.id)
                                    if not member:
                                        try:
                                            member = await after.guild.fetch_member(user.id)
                                        except discord.HTTPException:
                                            pass
                                    if member:
                                        operator_mention = f"{member.mention} ({member.name})"
                                    else:
                                        operator_mention = f"{user.name} (ID: {user.id})"
                                    break
                    except Exception:
                        logger.exception("名前変更の監査ログ取得に失敗しました。")

                    if should_send_log:
                        try:
                            embed = discord.Embed(
                                title="スレッド名変更ログ",
                                color=discord.Color.blue(),
                                timestamp=datetime.now(timezone.utc)
                            )
                            embed.add_field(name="スレッド", value=f"{after.mention} ({after.name})", inline=False)
                            embed.add_field(name="変更前", value=before.name, inline=False)
                            embed.add_field(name="変更後", value=after.name, inline=False)
                            embed.add_field(name="実行者", value=operator_mention, inline=False)
                            await log_channel.send(embed=embed)
                        except Exception:
                            logger.exception("名前変更ログの送信に失敗しました。")

        if before.archived != after.archived or before.name != after.name:
            self.bot.schedule_panel_update(parent_channel)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        parent_channel = thread.parent
        if not parent_channel:
            parent_channel = thread.guild.get_channel(thread.parent_id)
        if not parent_channel:
            try:
                parent_channel = await thread.guild.fetch_channel(thread.parent_id)
            except discord.HTTPException:
                pass

        if not parent_channel:
            return

        async with self.bot.db.execute('SELECT channel_id FROM thread_panels WHERE channel_id = ?', (thread.parent_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return

        self.bot.schedule_panel_update(parent_channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if isinstance(message.channel, discord.Thread):
            if message.author.id != self.bot.user.id:
                await self.bot.db.execute('DELETE FROM thread_extensions WHERE thread_id = ?', (message.channel.id,))
                await self.bot.db.commit()
                parent_channel = message.channel.parent
                if not parent_channel:
                    parent_channel = message.channel.guild.get_channel(message.channel.parent_id)
                if not parent_channel:
                    try:
                        parent_channel = await message.channel.guild.fetch_channel(message.channel.parent_id)
                    except discord.HTTPException:
                        pass

                if parent_channel:
                    async with self.bot.db.execute('SELECT 1 FROM thread_panels WHERE channel_id = ?', (parent_channel.id,)) as cursor:
                        is_managed = await cursor.fetchone()
                    if is_managed:
                        await self.reopen_thread_notification(message.channel, parent_channel, message.author)
            return

        async with self.bot.db.execute('SELECT panel_message_id FROM thread_panels WHERE channel_id = ?', (message.channel.id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return

        panel_message_id = row[0]

        if message.is_system() or message.type == discord.MessageType.thread_created:
            try:
                await message.delete()
            except Exception:
                pass
            return

        if message.author.id == self.bot.user.id:
            if message.id == panel_message_id:
                return
            return

        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.NotFound:
            pass


async def setup(bot):
    await bot.add_cog(ThreadCog(bot))
