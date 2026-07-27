DOBRODELO /reject UPDATE

Copy all three files into the same paths in your repository:

pipeline.py
tools/reject_article.py
.github/workflows/reject-article.yml

Then commit + push to main.

On every NEW review PR, each new article will show:

Reject this article: /reject article-slug

To reject one article, add that exact command as a normal comment in the
Conversation tab of the PR.

The workflow removes only that article (and its generated local image),
updates the same PR branch, removes the article block from the PR description,
and leaves seen.json untouched so the rejected story does not return next run.

No AI/API call is made by rejection.
