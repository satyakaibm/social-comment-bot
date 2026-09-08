import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from requests.exceptions import ReadTimeout

from app import db, fetch, meta_client, post, social_fetch, youtube_client


class ExistingReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, 'DB_PATH', Path(self.temp.name) / 'comments.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def seed(self, platform):
        with db.connect() as conn:
            db.insert_comment(conn, comment_id=platform, platform=platform,
                              video_id='media', video_title='Title', author='viewer',
                              text='Jai Maa', published_at='', draft_reply='🙏')
            db.update_status(conn, platform, 'approved')

    def test_post_skips_existing_replies_and_preserves_failed_checks(self):
        for platform in ('youtube', 'facebook', 'instagram'):
            self.seed(platform)
            target = post if platform == 'youtube' else meta_client
            with patch.object(post.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', True), \
                 patch.object(post, 'get_client', return_value=MagicMock()), \
                 patch.object(post, 'get_my_channel_id', return_value='owner'), \
                 patch.object(post, 'get_video_channel_ids', return_value={'media':'owner'}), \
                 patch.object(target, 'find_own_reply', side_effect=RuntimeError('offline')), \
                 patch.object(meta_client, 'reply_to_comment') as send:
                self.assertEqual(post.post_approved(platform=platform), 0)
                send.assert_not_called()
            with db.connect() as conn:
                self.assertEqual(len(db.list_by_status(conn, 'approved')), 1)
            with patch.object(post.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', True), \
                 patch.object(post, 'get_client', return_value=MagicMock()) as yt, \
                 patch.object(post, 'get_my_channel_id', return_value='owner'), \
                 patch.object(post, 'get_video_channel_ids', return_value={'media':'owner'}), \
                 patch.object(target, 'find_own_reply', return_value='manual'), \
                 patch.object(meta_client, 'reply_to_comment') as send:
                self.assertEqual(post.post_approved(platform=platform), 0)
                send.assert_not_called()
                yt.return_value.comments.assert_not_called()
            with db.connect() as conn:
                row = conn.execute('SELECT * FROM comments WHERE comment_id=?', (platform,)).fetchone()
                self.assertEqual(row['status'], 'already_replied')
            self.assertEqual(row['reply_comment_id'], 'manual')

    def test_facebook_fast_mode_posts_without_a_remote_duplicate_check(self):
        self.seed('facebook')
        with patch.object(post.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', False), \
             patch.object(meta_client, 'find_own_reply') as find, \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(post.post_approved(platform='facebook'), 1)

        find.assert_not_called()
        send.assert_called_once_with('facebook', '🙏', platform='facebook')

    def test_recent_instagram_check_is_reused_for_its_first_post(self):
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id='instagram-recent', platform='instagram',
                video_id='media', video_title='Title', author='viewer',
                text='Jai Maa', published_at='', draft_reply='🙏',
                reply_checked_at=db.now(),
            )
            db.update_status(conn, 'instagram-recent', 'approved')
        with patch.object(meta_client, 'find_own_reply') as find, \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(post.post_approved(platform='instagram'), 1)

        find.assert_not_called()
        send.assert_called_once_with('instagram-recent', '🙏', platform='instagram')

    def test_youtube_post_checks_oauth_and_video_owner_identities(self):
        self.seed('youtube')
        youtube = MagicMock()
        with patch.object(post, 'get_client', return_value=youtube), \
             patch.object(post, 'get_my_channel_id', return_value='oauth-channel'), \
             patch.object(post, 'get_video_channel_ids', return_value={'media':'hindolroad'}), \
             patch.object(post, 'find_own_reply', return_value='manual') as find:
            self.assertEqual(post.post_approved(platform='youtube'), 0)

        find.assert_called_once_with(
            youtube, 'youtube', {'oauth-channel', 'hindolroad'}
        )
        youtube.comments.return_value.insert.assert_not_called()

    def test_youtube_identity_check_can_record_quota_without_locking(self):
        self.seed('youtube')
        youtube = MagicMock()

        def identity_with_quota(_):
            db.add_quota_usage('youtube', 'test', 1, 10000)
            return 'owner'

        with patch.object(post, 'get_client', return_value=youtube), \
             patch.object(post, 'get_my_channel_id', side_effect=identity_with_quota), \
             patch.object(post, 'get_video_channel_ids', return_value={'media': 'owner'}), \
             patch.object(post, 'find_own_reply', return_value='manual'):
            self.assertEqual(post.post_approved(platform='youtube'), 0)

        with db.connect() as conn:
            quota = db.get_quota_usage(conn, 'youtube', 'test')
            self.assertEqual(quota['used'], 1)

    def test_graph_error_marks_comment_failed(self):
        self.seed('facebook')
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(
                 meta_client, 'reply_to_comment',
                 side_effect=meta_client.GraphAPIError('token expired'),
             ):
            self.assertEqual(post.post_approved(platform='facebook'), 0)
        with db.connect() as conn:
            row = db.get_comment(conn, 'facebook')
            self.assertEqual(row['status'], 'failed')
            self.assertIn('token expired', row['error'])

    def test_facebook_retry_checks_for_existing_reply_even_in_fast_mode(self):
        # Reproduces the duplicate-reply bug: a comment that already errored
        # once must be verified before Facebook fast mode tries it again,
        # even though fast mode skips the check for a brand-new comment.
        self.seed('facebook')
        with db.connect() as conn:
            db.update_status(conn, 'facebook', 'failed', error='Meta rejected it once')
        with patch.object(post.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', False), \
             patch.object(meta_client, 'find_own_reply', return_value='already-posted') as find, \
             patch.object(meta_client, 'reply_to_comment') as send:
            self.assertEqual(
                post.post_approved(platform='facebook', include_failed=True), 0
            )
        find.assert_called_once()
        send.assert_not_called()
        with db.connect() as conn:
            row = db.get_comment(conn, 'facebook')
            self.assertEqual(row['status'], 'already_replied')
            self.assertEqual(row['reply_comment_id'], 'already-posted')

    def test_repeated_failures_stop_auto_retrying_after_max_attempts(self):
        self.seed('facebook')
        with patch.object(post.config, 'META_MAX_POST_ATTEMPTS', 3), \
             patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(
                 meta_client, 'reply_to_comment',
                 side_effect=meta_client.GraphAPIError('comment not added'),
             ):
            for _ in range(3):
                self.assertEqual(
                    post.post_approved(platform='facebook', include_failed=True), 0
                )
        with db.connect() as conn:
            row = db.get_comment(conn, 'facebook')
            self.assertEqual(row['status'], 'rejected')
            self.assertIn('Gave up after 3 failed attempts', row['error'])

        # A row that has given up is 'rejected', not 'failed', so it drops
        # out of --retry-failed automatically.
        with patch.object(meta_client, 'find_own_reply') as find, \
             patch.object(meta_client, 'reply_to_comment') as send:
            self.assertEqual(
                post.post_approved(platform='facebook', include_failed=True), 0
            )
        find.assert_not_called()
        send.assert_not_called()

    def test_old_comment_is_rejected_without_remote_checks_or_posting(self):
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id='old-facebook', platform='facebook',
                video_id='media', video_title='Title', author='viewer',
                text='Old comment', published_at='2020-01-01T00:00:00Z',
                draft_reply='🙏',
            )
            db.update_status(conn, 'old-facebook', 'approved')

        with patch.object(meta_client, 'find_own_reply') as find, \
             patch.object(meta_client, 'reply_to_comment') as send, \
             patch.object(meta_client, 'like_comment') as like:
            self.assertEqual(
                post.post_approved(platform='facebook', like_comments=True), 0
            )
            find.assert_not_called()
            send.assert_not_called()
            like.assert_not_called()

        with db.connect() as conn:
            row = db.get_comment(conn, 'old-facebook')
            self.assertEqual(row['status'], 'rejected')
            self.assertIn('older than 90 days', row['error'])

    def test_repeated_graph_errors_stop_the_platform_batch(self):
        with db.connect() as conn:
            for index in range(5):
                comment_id = f'instagram-{index}'
                db.insert_comment(
                    conn, comment_id=comment_id, platform='instagram',
                    video_id='media', video_title='Title', author='viewer',
                    text='Jai Maa', published_at='', draft_reply='🙏',
                )
                db.update_status(conn, comment_id, 'approved')

        with patch.object(post.config, 'PUBLISH_ERROR_LIMIT', 3), \
             patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(
                 meta_client, 'reply_to_comment',
                 side_effect=meta_client.GraphAPIError('comment not added'),
             ) as send:
            self.assertEqual(post.post_approved(platform='instagram'), 0)

        self.assertEqual(send.call_count, 3)
        with db.connect() as conn:
            statuses = {
                row['status']
                for row in conn.execute(
                    "SELECT status FROM comments WHERE platform = 'instagram'"
                )
            }
            self.assertEqual(statuses, {'failed', 'approved'})

    def test_daily_reply_limit_stops_posting_once_reached(self):
        with db.connect() as conn:
            for index in range(4):
                comment_id = f'facebook-{index}'
                db.insert_comment(
                    conn, comment_id=comment_id, platform='facebook',
                    video_id='media', video_title='Title', author='viewer',
                    text='Jai Maa', published_at='', draft_reply='🙏',
                )
                db.update_status(conn, comment_id, 'approved')

        with patch.object(post.config, 'FACEBOOK_DAILY_REPLY_LIMIT', 2), \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(post.post_approved(platform='facebook'), 2)

        self.assertEqual(send.call_count, 2)
        with db.connect() as conn:
            statuses = {
                row['comment_id']: row['status']
                for row in conn.execute(
                    "SELECT comment_id, status FROM comments WHERE platform = 'facebook'"
                )
            }
        self.assertEqual(list(statuses.values()).count('posted'), 2)
        self.assertEqual(list(statuses.values()).count('approved'), 2)

        # The cap already counts today's earlier posts, so a second run
        # posts nothing more today.
        with patch.object(post.config, 'FACEBOOK_DAILY_REPLY_LIMIT', 2), \
             patch.object(meta_client, 'reply_to_comment') as send:
            self.assertEqual(post.post_approved(platform='facebook'), 0)
        send.assert_not_called()

    def test_failed_reply_is_checked_and_retried(self):
        self.seed('instagram')
        with db.connect() as conn:
            db.update_status(
                conn, 'instagram', 'failed',
                draft_reply='@viewer {"reply": 🙏 }}', error='interrupted',
            )
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(
                post.post_approved(platform='instagram', include_failed=True), 1
            )

        send.assert_called_once_with('instagram', '@viewer 🙏', platform='instagram')
        with db.connect() as conn:
            row = db.get_comment(conn, 'instagram')
            self.assertEqual(row['status'], 'posted')
            self.assertEqual(row['error'], '')

    def test_failed_reply_is_not_duplicated_when_remote_reply_exists(self):
        self.seed('instagram')
        with db.connect() as conn:
            db.update_status(conn, 'instagram', 'failed', error='interrupted')
        with patch.object(meta_client, 'find_own_reply', return_value='remote'), \
             patch.object(meta_client, 'reply_to_comment') as send:
            self.assertEqual(
                post.post_approved(platform='instagram', include_failed=True), 0
            )

        send.assert_not_called()
        with db.connect() as conn:
            row = db.get_comment(conn, 'instagram')
            self.assertEqual(row['status'], 'already_replied')
            self.assertEqual(row['reply_comment_id'], 'remote')
            self.assertEqual(row['error'], '')

    def test_atomic_claim_allows_only_one_publisher(self):
        self.seed('instagram')
        with db.connect() as first:
            self.assertTrue(
                db.claim_comment_for_post(first, 'instagram', 'approved')
            )
        with db.connect() as second:
            self.assertFalse(
                db.claim_comment_for_post(second, 'instagram', 'approved')
            )
            self.assertEqual(
                db.get_comment(second, 'instagram')['status'], 'posting'
            )

    def test_publisher_skips_row_claimed_by_another_process(self):
        self.seed('instagram')
        with patch.object(db, 'claim_comment_for_post', return_value=False), \
             patch.object(meta_client, 'find_own_reply') as find, \
             patch.object(meta_client, 'reply_to_comment') as send:
            self.assertEqual(post.post_approved(platform='instagram'), 0)

        find.assert_not_called()
        send.assert_not_called()

    def test_meta_write_timeout_stays_pending_for_duplicate_check(self):
        self.seed('facebook')
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(
                 meta_client, 'reply_to_comment',
                 side_effect=ReadTimeout('read timed out'),
             ):
            self.assertEqual(post.post_approved(platform='facebook'), 0)
        with db.connect() as conn:
            row = db.get_comment(conn, 'facebook')
            self.assertEqual(row['status'], 'approved')
            self.assertIsNone(row['reply_comment_id'])

    def test_facebook_post_poll_is_limited(self):
        with patch.object(meta_client.config, 'FACEBOOK_POST_IDS', []), \
             patch.object(meta_client.config, 'FACEBOOK_POST_LIMIT', 2), \
             patch.object(
                 meta_client, 'iter_paged',
                 return_value=iter([{'id':'p1'}, {'id':'p2'}, {'id':'p3'}]),
             ) as pages:
            self.assertEqual(list(meta_client.iter_facebook_post_ids()), ['p1', 'p2'])
        pages.assert_called_once_with(
            f'{meta_client.config.FACEBOOK_PAGE_ID}/posts', fields='id', limit=2
        )

    def test_unanswered_comment_still_posts(self):
        self.seed('facebook')
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(post.post_approved(platform='facebook'), 1)
            send.assert_called_once_with('facebook', '🙏', platform='facebook')

    def test_malformed_json_draft_is_cleaned_before_instagram_post(self):
        self.seed('instagram')
        malformed = '@viewer {"reply": ଜୟ ମା ଦକ୍ଷିଣକାଳୀ! }}'
        with db.connect() as conn:
            db.update_status(
                conn, 'instagram', 'approved', draft_reply=malformed
            )
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(post.post_approved(platform='instagram'), 1)

        cleaned = '@viewer ଜୟ ମା ଦକ୍ଷିଣକାଳୀ!'
        send.assert_called_once_with('instagram', cleaned, platform='instagram')
        with db.connect() as conn:
            self.assertEqual(db.get_comment(conn, 'instagram')['draft_reply'], cleaned)

    def test_instagram_reply_can_like_original_comment(self):
        self.seed('instagram')
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'reply_to_comment', return_value='new'), \
             patch.object(meta_client, 'like_comment') as like:
            self.assertEqual(
                post.post_approved(platform='instagram', like_comments=True), 1
            )
            like.assert_called_once_with('instagram', platform='instagram')

    def test_replies_are_posted_before_likes_begin(self):
        for comment_id in ('facebook-first', 'facebook-second'):
            with db.connect() as conn:
                db.insert_comment(
                    conn, comment_id=comment_id, platform='facebook',
                    video_id='media', video_title='Title', author='viewer',
                    text='Jai Maa', published_at='', draft_reply='🙏',
                )
                db.update_status(conn, comment_id, 'approved')
        events = []
        with patch.object(post.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', False), \
             patch.object(
                 meta_client, 'reply_to_comment',
                 side_effect=lambda *args, **kwargs: events.append('reply') or 'new',
             ), \
             patch.object(
                 meta_client, 'like_comment',
                 side_effect=lambda *args, **kwargs: events.append('like'),
             ):
            self.assertEqual(
                post.post_approved(platform='facebook', like_comments=True), 2
            )
        self.assertEqual(events, ['reply', 'reply', 'like', 'like'])

    def test_instagram_likes_use_user_token_query(self):
        response = MagicMock()
        response.json.return_value = {'success': True}
        with patch.object(meta_client.config, 'INSTAGRAM_USER_ID', 'ig-user'), \
             patch.object(meta_client, 'get_user_access_token', return_value='user-token'), \
             patch.object(meta_client, 'graph_post') as page_post, \
             patch.object(meta_client._http_session, 'post', return_value=response) as send:
            meta_client.like_comment('ig-comment', platform='instagram')
        page_post.assert_not_called()
        send.assert_called_once()
        url = send.call_args.args[0]
        params = send.call_args.kwargs['params']
        self.assertTrue(url.endswith('/ig-user/likes'))
        self.assertEqual(params['comment_id'], 'ig-comment')
        self.assertEqual(params['access_token'], 'user-token')

    def test_instagram_likes_reject_page_token(self):
        meta_client._user_token_cache = None
        self.addCleanup(setattr, meta_client, '_user_token_cache', None)
        identity = MagicMock()
        identity.json.return_value = {'id': 'page'}
        with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'page'), \
             patch.object(meta_client.config, 'FACEBOOK_PAGE_ACCESS_TOKEN', 'page-token'), \
             patch.object(meta_client._http_session, 'get', return_value=identity):
            with self.assertRaisesRegex(meta_client.GraphAPIError, 'User access token'):
                meta_client.get_user_access_token()

    def test_user_token_is_the_configured_non_page_token(self):
        meta_client._user_token_cache = None
        self.addCleanup(setattr, meta_client, '_user_token_cache', None)
        identity = MagicMock()
        identity.json.return_value = {'id': 'user'}
        with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'page'), \
             patch.object(meta_client.config, 'FACEBOOK_PAGE_ACCESS_TOKEN', 'user-token'), \
             patch.object(meta_client._http_session, 'get', return_value=identity) as get:
            self.assertEqual(meta_client.get_user_access_token(), 'user-token')
            self.assertEqual(meta_client.get_user_access_token(), 'user-token')
            get.assert_called_once()

    def test_facebook_likes_use_comment_likes_edge(self):
        with patch.object(meta_client, 'graph_post', return_value={'success': True}) as send:
            meta_client.like_comment('fb-comment', platform='facebook')
        send.assert_called_once_with('fb-comment/likes')

    def test_graph_post_retries_meta_code_one_once(self):
        temporary = MagicMock(headers={})
        temporary.json.return_value = {
            'error': {
                'code': 1,
                'message': "Please reduce the amount of data you're asking for",
            }
        }
        success = MagicMock(headers={})
        success.json.return_value = {'id': 'new-reply'}
        with patch.object(meta_client, 'get_page_access_token', return_value='token'), \
             patch.object(meta_client._http_session, 'post', side_effect=[temporary, success]) as send, \
             patch.object(meta_client.time, 'sleep') as sleep, \
             patch.object(meta_client.config, 'META_POST_RETRIES', 2), \
             patch.object(meta_client.config, 'META_POST_RETRY_DELAY_SECONDS', 2):
            self.assertEqual(
                meta_client.graph_post('comment/comments', message='Thank you!'),
                {'id': 'new-reply'},
            )

        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_reply_to_comment_does_not_blindly_retry_on_temporary_error(self):
        # Code 1 can follow a write that actually succeeded server-side, so
        # resending it (as graph_post does for idempotent edges) risks
        # creating a second, duplicate public reply. Posting a reply must
        # send the write exactly once and surface the error instead.
        temporary = MagicMock(headers={})
        temporary.json.return_value = {
            'error': {
                'code': 1,
                'message': "Please reduce the amount of data you're asking for",
            }
        }
        with patch.object(meta_client, 'get_page_access_token', return_value='token'), \
             patch.object(meta_client._http_session, 'post', return_value=temporary) as send, \
             patch.object(meta_client.time, 'sleep') as sleep:
            with self.assertRaises(meta_client.GraphAPIError):
                meta_client.reply_to_comment('comment', 'Thank you!', platform='facebook')

        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()

    def test_youtube_comment_likes_are_skipped(self):
        self.seed('youtube')
        youtube = MagicMock()
        youtube.comments.return_value.insert.return_value.execute.return_value = {
            'id': 'new',
        }
        with patch.object(post, 'get_client', return_value=youtube), \
             patch.object(post, 'get_my_channel_id', return_value='owner'), \
             patch.object(post, 'get_video_channel_ids', return_value={'media': 'owner'}), \
             patch.object(post, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'like_comment') as like:
            self.assertEqual(
                post.post_approved(platform='youtube', like_comments=True), 1
            )
            like.assert_not_called()

    def test_youtube_post_strips_an_old_automated_author_prefix(self):
        self.seed('youtube')
        with db.connect() as conn:
            db.update_status(conn, 'youtube', 'approved', draft_reply='@@viewer 🙏')
        youtube = MagicMock()
        youtube.comments.return_value.insert.return_value.execute.return_value = {'id': 'new'}
        with patch.object(post, 'get_client', return_value=youtube), \
             patch.object(post, 'get_my_channel_id', return_value='owner'), \
             patch.object(post, 'get_video_channel_ids', return_value={'media': 'owner'}), \
             patch.object(post, 'find_own_reply', return_value=None):
            self.assertEqual(post.post_approved(platform='youtube'), 1)

        body = youtube.comments.return_value.insert.call_args.kwargs['body']
        self.assertEqual(body['snippet']['textOriginal'], '🙏')

    def test_youtube_checks_later_reply_pages(self):
        youtube = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.execute.return_value = {'items': [{'id':'other', 'snippet':{'authorChannelId':{'value':'viewer'}}}]}
        second.execute.return_value = {'items': [{'id':'mine', 'snippet':{'authorChannelId':{'value':'owner'}}}]}
        youtube.comments.return_value.list.return_value = first
        youtube.comments.return_value.list_next.return_value = second
        self.assertEqual(youtube_client.find_own_reply(youtube, 'parent', 'owner'), 'mine')

    def test_youtube_treats_video_owner_reply_as_own(self):
        youtube = MagicMock()
        request = youtube.comments.return_value.list.return_value
        request.execute.return_value = {
            'items': [{
                'id': 'manual-hindolroad-reply',
                'snippet': {'authorChannelId': {'value': 'video-owner'}},
            }]
        }
        youtube.comments.return_value.list_next.return_value = None

        reply = youtube_client.find_own_reply(
            youtube, 'parent', {'oauth-channel', 'video-owner'}
        )

        self.assertEqual(reply, 'manual-hindolroad-reply')

    def test_video_owner_ids_are_batched(self):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            'items': [
                {'id': 'video-1', 'snippet': {'channelId': 'hindolroad'}},
                {'id': 'video-2', 'snippet': {'channelId': 'hindolroad'}},
            ]
        }

        owners = youtube_client.get_video_channel_ids(
            youtube, ['video-1', 'video-2', 'video-1']
        )

        self.assertEqual(owners, {'video-1': 'hindolroad', 'video-2': 'hindolroad'})
        youtube.videos.return_value.list.assert_called_once_with(
            part='snippet', id='video-1,video-2'
        )

    def test_meta_checks_later_reply_pages(self):
        for platform, fields, identity in (
            ('facebook', 'id,from', {'from': {'id':'owner'}}),
            ('instagram', 'id,username', {'username':'OWNER'}),
        ):
            with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'owner'), \
                 patch.object(meta_client, 'get_instagram_username', return_value='owner'), \
                 patch.object(meta_client, 'graph_get', return_value={'data':[], 'paging':{'next':'https://graph.facebook.com/next'}}), \
                 patch.object(meta_client._http_session, 'get') as get:
                get.return_value.json.return_value = {'data':[{'id':'mine', **identity}]}
                self.assertEqual(meta_client.find_own_reply('parent', platform=platform), 'mine')
                get.assert_called_once()

    def test_facebook_uses_independent_lookup_when_comments_edge_misses_reply(self):
        with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'owner'), \
             patch.object(meta_client, 'iter_paged', return_value=iter([])), \
             patch.object(
                 meta_client,
                 'graph_get',
                 return_value={
                     'comments': {
                         'data': [
                             {'id': 'viewer-reply', 'from': {'id': 'viewer'}},
                             {'id': 'page-reply', 'from': {'id': 'owner'}},
                         ]
                     }
                 },
             ) as get:
            self.assertEqual(
                meta_client.find_own_reply('parent', platform='facebook'),
                'page-reply',
            )

        get.assert_called_once_with(
            'parent', fields='comments.limit(100){id,from}'
        )

    def test_facebook_does_not_run_fallback_when_edge_finds_reply(self):
        with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'owner'), \
             patch.object(
                 meta_client,
                 'iter_paged',
                 return_value=iter([{'id': 'page-reply', 'from': {'id': 'owner'}}]),
             ) as pages, \
             patch.object(meta_client, 'graph_get') as get:
            self.assertEqual(
                meta_client.find_own_reply('parent', platform='facebook'),
                'page-reply',
            )

        get.assert_not_called()
        pages.assert_called_once_with(
            'parent/comments', fields='id,from', filter='stream', limit=100
        )

    def test_meta_reply_endpoints(self):
        with patch.object(meta_client, 'graph_post', return_value={'id':'new'}) as send:
            for platform, edge in (('facebook','comments'), ('instagram','replies')):
                meta_client.reply_to_comment('parent', '🙏', platform=platform)
                send.assert_called_with(
                    f'parent/{edge}', message='🙏', retry_on_temporary_error=False
                )

    def test_configured_page_token_skips_user_token_exchange(self):
        meta_client._page_token_cache = None
        self.addCleanup(setattr, meta_client, '_page_token_cache', None)
        identity = MagicMock()
        identity.json.return_value = {'id': 'page'}
        with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'page'), \
             patch.object(meta_client.config, 'FACEBOOK_PAGE_ACCESS_TOKEN', 'token'), \
             patch.object(meta_client._http_session, 'get', return_value=identity) as get:
            self.assertEqual(meta_client.get_page_access_token(), 'token')
            get.assert_called_once()
            self.assertTrue(get.call_args.args[0].endswith('/me'))

    def test_poll_skips_manual_replies_without_generating(self):
        for platform in ('facebook', 'instagram'):
            poll = getattr(social_fetch, f'poll_{platform}_and_draft')
            containers = 'iter_facebook_post_ids' if platform == 'facebook' else 'iter_instagram_media_ids'
            comments = 'iter_facebook_post_comments' if platform == 'facebook' else 'iter_instagram_media_comments'
            title = 'get_facebook_post_message' if platform == 'facebook' else 'get_instagram_media_caption'
            with patch.object(social_fetch.config, 'require'), \
                 patch.object(social_fetch.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', True), \
                 patch.object(meta_client, containers, return_value=['media']), \
                 patch.object(meta_client, comments, return_value=[{'id':platform, 'text':'Jai Maa', 'message':'Jai Maa', 'username':'viewer'}]), \
                 patch.object(meta_client, title, return_value='Title'), \
                 patch.object(meta_client, 'get_instagram_username', return_value='owner'), \
                 patch.object(meta_client, 'find_own_reply', return_value='manual'), \
                 patch.object(social_fetch, 'draft_reply') as draft:
                self.assertEqual(poll(), 0)
                draft.assert_not_called()
        with patch.object(fetch, 'get_client'), \
             patch.object(fetch, 'get_my_channel_id', return_value='owner'), \
             patch.object(fetch, 'get_uploads_playlist_id', return_value='uploads'), \
             patch.object(fetch, 'iter_uploaded_video_ids', return_value=iter([])), \
             patch.object(fetch.config, 'YOUTUBE_VIDEO_IDS', ['media']), \
             patch.object(fetch, '_video_title_cache', return_value=lambda _: 'Title'), \
             patch.object(fetch, '_iter_top_level_threads', return_value=[{'snippet':{'topLevelComment':{'id':'youtube', 'snippet':{'videoId':'media'}}}}]), \
             patch.object(fetch, 'find_own_reply', return_value='manual'), \
             patch.object(fetch, 'draft_reply') as draft:
            self.assertEqual(fetch.poll_and_draft(), 0)
            draft.assert_not_called()
        with db.connect() as conn:
            self.assertEqual(len(db.list_by_status(conn, 'already_replied')), 2)
            self.assertIsNone(db.get_comment(conn, 'youtube'))

    def test_youtube_poll_stops_drafting_once_daily_gemini_cap_is_reached(self):
        threads = [
            {'snippet': {'topLevelComment': {
                'id': f'youtube-{i}', 'snippet': {'videoId': 'media'},
            }}}
            for i in range(3)
        ]
        with patch.object(fetch, 'get_client'), \
             patch.object(fetch, 'get_my_channel_id', return_value='owner'), \
             patch.object(fetch, 'get_uploads_playlist_id', return_value='uploads'), \
             patch.object(fetch, 'iter_uploaded_video_ids', return_value=iter([])), \
             patch.object(fetch.config, 'YOUTUBE_VIDEO_IDS', ['media']), \
             patch.object(fetch.config, 'GEMINI_DAILY_DRAFT_LIMIT', 1), \
             patch.object(fetch, '_video_title_cache', return_value=lambda _: 'Title'), \
             patch.object(fetch, '_iter_top_level_threads', return_value=threads), \
             patch.object(fetch, 'find_own_reply', return_value=None), \
             patch.object(fetch, 'draft_reply', return_value='🙏') as draft:
            self.assertEqual(fetch.poll_and_draft(), 1)
        draft.assert_called_once()
        with db.connect() as conn:
            self.assertIsNotNone(db.get_comment(conn, 'youtube-0'))
            self.assertIsNone(db.get_comment(conn, 'youtube-1'))
            self.assertIsNone(db.get_comment(conn, 'youtube-2'))

    def test_facebook_poll_stops_drafting_once_daily_gemini_cap_is_reached(self):
        comments = [
            {'id': f'facebook-{i}', 'message': 'Jai Maa', 'from': {'name': 'viewer'}}
            for i in range(3)
        ]
        with patch.object(social_fetch.config, 'require'), \
             patch.object(social_fetch.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', False), \
             patch.object(social_fetch.config, 'GEMINI_DAILY_DRAFT_LIMIT', 1), \
             patch.object(meta_client, 'iter_facebook_post_ids', return_value=['post']), \
             patch.object(meta_client, 'iter_facebook_post_comments', return_value=comments), \
             patch.object(meta_client, 'get_facebook_post_message', return_value='Title'), \
             patch.object(social_fetch, 'draft_reply', return_value='🙏') as draft:
            self.assertEqual(social_fetch.poll_facebook_and_draft(), 1)
        draft.assert_called_once()
        with db.connect() as conn:
            self.assertIsNotNone(db.get_comment(conn, 'facebook-0'))
            self.assertIsNone(db.get_comment(conn, 'facebook-1'))
            self.assertIsNone(db.get_comment(conn, 'facebook-2'))

        # Already-drafted comments count toward the cap on a later call too.
        with patch.object(social_fetch.config, 'require'), \
             patch.object(social_fetch.config, 'FACEBOOK_VERIFY_EXISTING_REPLIES', False), \
             patch.object(social_fetch.config, 'GEMINI_DAILY_DRAFT_LIMIT', 1), \
             patch.object(meta_client, 'iter_facebook_post_ids', return_value=['post']), \
             patch.object(meta_client, 'iter_facebook_post_comments', return_value=comments), \
             patch.object(meta_client, 'get_facebook_post_message', return_value='Title'), \
             patch.object(social_fetch, 'draft_reply') as draft:
            self.assertEqual(social_fetch.poll_facebook_and_draft(), 0)
        draft.assert_not_called()

    def test_configured_youtube_ids_are_added_to_latest_uploads(self):
        with patch.object(fetch, 'get_client'), \
             patch.object(fetch, 'get_my_channel_id', return_value='owner'), \
             patch.object(fetch, 'get_uploads_playlist_id', return_value='uploads'), \
             patch.object(
                 fetch, 'iter_uploaded_video_ids',
                 return_value=iter(['latest-1', 'configured', 'latest-2']),
             ), \
             patch.object(fetch.config, 'YOUTUBE_VIDEO_IDS', ['configured']), \
             patch.object(fetch.config, 'YOUTUBE_VIDEO_LIMIT', 3), \
             patch.object(fetch.config, 'YOUTUBE_COMMENT_LIMIT', 3), \
             patch.object(fetch, '_iter_top_level_threads', return_value=[]) as threads:
            self.assertEqual(fetch.poll_and_draft(), 0)

        self.assertEqual(
            [call.kwargs['video_id'] for call in threads.call_args_list],
            ['configured', 'latest-1', 'latest-2'],
        )


if __name__ == '__main__':
    unittest.main()
