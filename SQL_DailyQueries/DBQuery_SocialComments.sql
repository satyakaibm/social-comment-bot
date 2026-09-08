SELECT platform, comment_id, author, text, draft_reply, status, reply_comment_id,
    datetime(updated_at, '+5 hours', '+30 minutes') AS posted_at_ist
FROM comments
WHERE status IN ('posted', 'already_replied')
ORDER BY posted_at_ist DESC
LIMIT 100;

--To confirm the count:
SELECT platform, status, COUNT(*) AS total
FROM comments
GROUP BY platform, status
ORDER BY platform, status;

--It should return the 200 YouTube comments marked as posted.
SELECT comment_id, video_id, video_title, author, text AS original_comment, draft_reply AS posted_reply, reply_comment_id,
    datetime(updated_at, '+5 hours', '+30 minutes') AS posted_at_ist
FROM comments
WHERE platform = 'youtube'
  AND status = 'posted'
ORDER BY posted_at_ist DESC;

--It should return the 200 Instragram comments marked as posted.
SELECT comment_id, video_id, video_title, author, text AS original_comment, draft_reply AS posted_reply, reply_comment_id,
    datetime(updated_at, '+5 hours', '+30 minutes') AS posted_at_ist
FROM comments
WHERE platform = 'instagram' AND status = 'posted'
ORDER BY posted_at_ist DESC;

-- To show only the latest 50 reply in youtube:
SELECT author, text AS original_comment, draft_reply AS posted_reply, video_title,
    datetime(updated_at, '+5 hours', '+30 minutes') AS posted_at_ist
FROM comments
WHERE platform = 'instagram' AND status = 'posted'
ORDER BY posted_at_ist DESC
LIMIT 50;
