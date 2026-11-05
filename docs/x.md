# X (Twitter) source

Adapter: `src/sources/adapters/x.ts`. Kind `x`, modes `home` and `user`.

The official API is paid and scraping breaks the terms of service, so this source renders the
owner's own logged-in timeline in Cloudflare Browser Rendering, with a throwaway account. It is a
best-effort source: a failure here never aborts the digest (FR1.5, QR2). Replies are not fetched.

## What it fetches

| Mode   | Page rendered            | Notes                                                            |
| ------ | ------------------------ | ---------------------------------------------------------------- |
| `home` | `https://x.com/home`     | Whichever tab the account last used ("For you" or "Following")   |
| `user` | `https://x.com/{handle}` | Requires `handle` in the source config; a leading `@` is dropped |

For each run:

1. The timeline is rendered with the session cookies and scrolled three times.
2. An extract script reads every `article[data-testid="tweet"]`: the tweet id and author handle
   from the permalink (`/{handle}/status/{id}`, the link that wraps the `<time>` element), the
   text from `[data-testid="tweetText"]`, the timestamp from `<time datetime>`, and the
   `aria-label`s of the reply, repost and like buttons plus the engagement group.
3. Each tweet becomes a `SourceItem` of kind `commentary`. The canonical URL is
   `https://x.com/{handle}/status/{id}` and the item id is its sha-256. Engagement maps likes to
   `likes`, replies to `comments` and reposts to `shares`. The title is the first line of the
   text (80 characters); language is `zh` when the text contains Han characters.
4. Duplicate cards from scrolling are dropped and at most `MAX_ITEMS_PER_SOURCE` items are kept.

The "Following" tab has no URL of its own; select it once in the browser session whose cookie is
used, X remembers the choice per account.

## Obtaining the cookie

The adapter needs the raw `Cookie` request header of a logged-in session, stored as the
`X_COOKIE` secret and exposed to the code as `ctx.secrets.xCookie`.

1. Log in at `https://x.com` in a desktop browser with the throwaway account.
2. Open the developer tools, Network tab, and reload `https://x.com/home`.
3. Select the document request to `x.com/home`, open Request Headers and copy the whole value of
   the `Cookie` header. The entries that matter are `auth_token` and `ct0`; copying everything is
   fine.
4. Store it: `npx wrangler secret put X_COOKIE` and paste the value.

## Expected breakage modes

| Symptom                                         | Result                                    | Likely cause                                                                                                |
| ----------------------------------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `not_configured`                                | Source skipped                            | Browser Rendering binding absent, cookie secret empty, or mode `user` without `handle`                      |
| `blocked`                                       | Source skipped, reported in Source health | Session expired (redirect to `/i/flow/login`), "Sign in to X" interstitial, captcha or account lock         |
| `fetch_failed`                                  | Source skipped                            | Page timeout (30 s), navigation failure, Browser Rendering limits                                           |
| `parse_failed`                                  | Source skipped                            | The extract script returned something that is not a list of cards: a `data-testid` was renamed              |
| Empty result with no error                      | Nothing from this source today            | The timeline rendered but no card matched `article[data-testid="tweet"]`, or the account sees an empty feed |
| Items with `@handle` as author and no timestamp | Degraded metadata                         | `User-Name` or `<time>` markup changed                                                                      |

Every selector and URL lives in the `SELECTORS` object at the top of the adapter; fix it there.
The unit tests feed canned extraction results through the fake browser, so they keep passing while
you repair the real selectors.

## Refreshing the cookie

`auth_token` lasts months unless the account logs out or X invalidates sessions. When the digest
footer reports `blocked` for this source, repeat the steps above and run
`npx wrangler secret put X_COOKIE` again; no deploy is needed. If the throwaway account gets
locked, create a new one and copy its cookie instead.

## Limits

- One page render per run with three scrolls, well below any platform limit (QR1).
- Content is read for the owner only and never republished.
