import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            with patch.object(post, 'get_client', return_value=MagicMock()), \
                 patch.object(post, 'get_my_channel_id', return_value='owner'), \
                 patch.object(target, 'find_own_reply', side_effect=RuntimeError('offline')), \
                 patch.object(meta_client, 'reply_to_comment') as send:
                self.assertEqual(post.post_approved(platform=platform), 0)
                send.assert_not_called()
            with db.connect() as conn:
                self.assertEqual(len(db.list_by_status(conn, 'approved')), 1)
            with patch.object(post, 'get_client', return_value=MagicMock()) as yt, \
                 patch.object(post, 'get_my_channel_id', return_value='owner'), \
                 patch.object(target, 'find_own_reply', return_value='manual'), \
                 patch.object(meta_client, 'reply_to_comment') as send:
                self.assertEqual(post.post_approved(platform=platform), 0)
                send.assert_not_called()
                yt.return_value.comments.assert_not_called()
            with db.connect() as conn:
                row = conn.execute('SELECT * FROM comments WHERE comment_id=?', (platform,)).fetchone()
                self.assertEqual(row['status'], 'already_replied')
                self.assertEqual(row['reply_comment_id'], 'manual')

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

    def test_unanswered_comment_still_posts(self):
        self.seed('facebook')
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'reply_to_comment', return_value='new') as send:
            self.assertEqual(post.post_approved(platform='facebook'), 1)
            send.assert_called_once_with('facebook', '🙏', platform='facebook')

    def test_instagram_reply_can_like_original_comment(self):
        self.seed('instagram')
        with patch.object(meta_client, 'find_own_reply', return_value=None), \
             patch.object(meta_client, 'reply_to_comment', return_value='new'), \
             patch.object(meta_client, 'like_comment') as like:
            self.assertEqual(
                post.post_approved(platform='instagram', like_comments=True), 1
            )
            like.assert_called_once_with('instagram')

    def test_comment_likes_reject_youtube(self):
        with self.assertRaisesRegex(ValueError, 'facebook or instagram'):
            post.post_approved(platform='youtube', like_comments=True)

    def test_youtube_checks_later_reply_pages(self):
        youtube = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.execute.return_value = {'items': [{'id':'other', 'snippet':{'authorChannelId':{'value':'viewer'}}}]}
        second.execute.return_value = {'items': [{'id':'mine', 'snippet':{'authorChannelId':{'value':'owner'}}}]}
        youtube.comments.return_value.list.return_value = first
        youtube.comments.return_value.list_next.return_value = second
        self.assertEqual(youtube_client.find_own_reply(youtube, 'parent', 'owner'), 'mine')

    def test_meta_checks_later_reply_pages(self):
        for platform, fields, identity in (
            ('facebook', 'id,from', {'from': {'id':'owner'}}),
            ('instagram', 'id,username', {'username':'OWNER'}),
        ):
            with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'owner'), \
                 patch.object(meta_client, 'get_instagram_username', return_value='owner'), \
                 patch.object(meta_client, 'graph_get', return_value={'data':[], 'paging':{'next':'https://graph.facebook.com/next'}}), \
                 patch.object(meta_client.requests, 'get') as get:
                get.return_value.json.return_value = {'data':[{'id':'mine', **identity}]}
                self.assertEqual(meta_client.find_own_reply('parent', platform=platform), 'mine')
                get.assert_called_once()

    def test_meta_reply_endpoints(self):
        with patch.object(meta_client, 'graph_post', return_value={'id':'new'}) as send:
            for platform, edge in (('facebook','comments'), ('instagram','replies')):
                meta_client.reply_to_comment('parent', '🙏', platform=platform)
                send.assert_called_with(f'parent/{edge}', message='🙏')

    def test_configured_page_token_skips_user_token_exchange(self):
        meta_client._page_token_cache = None
        self.addCleanup(setattr, meta_client, '_page_token_cache', None)
        identity = MagicMock()
        identity.json.return_value = {'id': 'page'}
        with patch.object(meta_client.config, 'FACEBOOK_PAGE_ID', 'page'), \
             patch.object(meta_client.config, 'FACEBOOK_PAGE_ACCESS_TOKEN', 'token'), \
             patch.object(meta_client.requests, 'get', return_value=identity) as get:
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
             patch.object(fetch.config, 'YOUTUBE_VIDEO_IDS', ['media']), \
             patch.object(fetch, '_video_title_cache', return_value=lambda _: 'Title'), \
             patch.object(fetch, '_iter_top_level_threads', return_value=[{'snippet':{'topLevelComment':{'id':'youtube', 'snippet':{'videoId':'media'}}}}]), \
             patch.object(fetch, 'find_own_reply', return_value='manual'), \
             patch.object(fetch, 'draft_reply') as draft:
            self.assertEqual(fetch.poll_and_draft(), 0)
            draft.assert_not_called()
        with db.connect() as conn:
            self.assertEqual(len(db.list_by_status(conn, 'already_replied')), 3)


if __name__ == '__main__':
    unittest.main()
