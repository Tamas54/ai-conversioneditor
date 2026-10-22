# Changelog

## 2026-10-22 — Big files: phone videos upload in pieces and resume; the agent never carries the bytes

The owner's question: a 2-minute iPhone 17 Pro Max video was 235 MB. "How do we get it into the app, and how does an
agent handle a file that big?"
- **The app uploads a large file (over 16 MB) in 8 MB pieces**, three at a time, with progress: "Uploading IMG_4821.MOV —
  37% (88 of 227 MB). Keep this page open."
  - A piece that fails is retried, and the upload waits for the connection to come back.
  - If it still stops, choosing the same file again carries on from where it stopped: the server lists what arrived
    (`GET /api/upload/chunks/{id}`).
  - Leaving the page while uploading asks first.
  - Measured locally with a browser: a 238 MB video, with an 8-second network cut at 30%, finished. After a page
    reload at 52%, the server already had 20 of 29 pieces and only the rest was sent. The file arrived byte for byte.
- **The server writes uploads to disk, not to memory.**
  - A 238 MB upload in one request raised the server's peak memory from 141 MB to 374 MB; now it stays at 142 MB.
  - A piece that arrives short no longer counts as received.
  - Pieces are joined in a background thread.
- **The agent never carries the bytes.**
  - `upload_from_url` downloads to disk, recognises a phone video (MOV/MP4) without a file name, and checks the file.
  - The base64 tools (`upload_file`, `upload_chunk`) are for small files, up to 50 MB in all. For anything bigger they
    tell the agent to ask the person to upload in the app or give a link, then find it with
    `files(action="list", kind="video")`.
  - From then on the agent works with the file_id: the server cuts, renders and transcribes, and the agent sees
    frames, the transcript and the numbers.

## 2026-10-22 — Fixes from the editorial trial (links, footnotes, clean copy, fact sources)

From an MCP agent's report after a full trial on a long study: a fact check, then a tracked rewrite with WordCraft.
- **WordCraft CLI 0.5.0 → 0.9.0.**
  - Text typed at the end of a link no longer becomes part of the link. In one trial version, 100 of 118 paragraphs
    pointed to the last source's website.
  - Accept All no longer gives the heading after deleted paragraphs the deleted paragraph's style.
  - The web editor stays on our own 0.5.0 build; the two share the .docx format.
- **The validator finds text that leaked into a link.** It reports an error when three or more paragraphs, or a long
  one, sit inside a link to a site the text doesn't name. `validate_document(fix=true)` proposes removing that link;
  links whose text names the site keep it.
- **Footnotes:**
  - The clean copy (`address [{"final": true}]`) and accept/reject now also cover changes inside footnotes,
    endnotes, headers and footers.
  - Footnotes of deleted text are removed, and the remaining ones are stored in reference order. LibreOffice matches
    footnotes by their order in the file, so a leftover note put the wrong text under a number.
  - The validator reports orphan or out-of-order footnotes, and the fix proposal tidies them.
  - Every WordCraft save tidies them too.
- **Accepting a fully deleted paragraph** removes it, and the following paragraph keeps its own style. This applies to
  both the engine and our own `final`.
- **`review.compare {"text"}` is done by the Studio.**
  - Footnotes, pictures, fields and comments stay in place, and the tracked changes carry the given `author`. The
    engine's own compare dropped the footnote and signed its changes as "Compare".
  - The text is compared line by line: a new line becomes a tracked paragraph, a missing line a tracked deletion.
    Lines are matched by similarity, so a line added at the top doesn't shift the pairs.
  - Table rows aren't added or removed this way; the result says so.
- **`select.text` also finds text in footnotes, endnotes, headers and footers.** It is turned into `select.range` in
  that note.
- **Read-only runs** (`caret.*`, `select.*`, `document.selection`, `document.text`, `document.paragraph`) no longer
  save a new version.
- **New comment on a file without comments** no longer fails with `'NoneType' object has no attribute 'iter'`.
- **Word counts follow the text as it reads after accepting** (tracked insertions and tables included). When tracked
  changes exist, `words_before_tracked_changes` is shown too. A full tracked rewrite showed 4201 → 361 before.
- **The validator no longer reports a short tracked heading as missing** when the layout shows the deleted text right
  next to it.
- **Fact check:**
  - Every source has a weight:
    - named: the publication the claim cites, e.g. ifr.org for "according to the IFR";
    - official;
    - secondary;
    - weak: forums, Q&A sites and social posts.
  - A weak source alone, or only a secondary site for a claim that names its source, gives "unverified" with
    `weak_support`, never "supported".
  - When search snippets don't contain the number, the top results are opened through the Echolot ENGINE, including
    PDFs and pages that answer 403. The verdict then sees the passages around the number.
  - The verdict must quote the source. If the quote isn't in the source, the verdict is "unverified"; this catches a
    figure tied to another country.
- **The file list no longer fails while a save is running.** WordCraft 0.9 keeps a hidden temporary file
  (`.wordcraft-save-*.tmp`) in the folder during a save; listing then failed with "Invalid file_id". Hidden files are
  now skipped there and in sharing.
- **Web: More ▾ → "Accept all changes, remove comments"** makes a clean copy as a new version (`/api/make/clean_copy`,
  no AI). A LibreOffice "accept all" save had left 289 changes and 22 comments in the file.

## 2026-10-22 — Editing modes with limits: ask first, propose as tracked changes; over MCP the agent writes

Owner's principle: over MCP the agent directs and thinks on its own tokens. The server gives the data, does the
deterministic work and the machine checks, and writes the file. The Studio's own AI runs over MCP only for the
exceptions: reading large books / scans, AI video, and AI pictures for clients that can't make them.
- **Editing modes over MCP:**
  1. Questions.
  2. With `brief`: a work package of numbered paragraphs plus the rules (`offset` pages long texts).
  3. With `changes=[{n, new, why, kind}]`: the server's guards, then tracked changes with the agent's reasons.

  No Studio AI step in any of these. The Studio's model writes the proposals only where there is no agent (web).
- **Social posts over MCP:**
  - `video(action="social")` returns the material: what the video says, chapters, keywords, people, credits, and each
    platform's limits and style.
  - The agent writes the posts and sends them back with `ai={"posts": {…}}`. The server checks the lengths and the
    numbers against the video, and adds chapters, credits and the .md.

The first piece of editorial intelligence, under the owner's principle: within limits, the system asks what the
author wants, and the person decides.
- **`edit_with_instruction(file_id, mode=…)`** with `proofread` (language only), `edit` (substantive: clearer, less
  repetition; content and position stay), `expand`, `shorten` or `rewrite`.
- **Without `brief`: the questions, and nothing changes.** No AI and no cost. Each question comes with a suggested
  default:
  - what the text is for and who reads it;
  - the limit (e.g. at most 10% shorter);
  - whether tables, numbers and quotations, and the author's position must stay;
  - words and names that must not change (detected candidates);
  - whether new thoughts are allowed;
  - which part to work on, and whose name goes on the changes.
- **With `brief={…}`: proposals, run in the background like any edit.**
  - The AI works paragraph by paragraph, one Studio AI step per ~6,000 characters, cached.
  - Machine guards: numbers and the named words must stay; proofreading may only change a little; a new thought is
    allowed only with permission; the whole-text length limit holds.
  - Whatever doesn't pass is listed in `held_back` with the reason.
  - Every change is a **tracked change** under the chosen name, with a comment saying why, in the document's
    language. A new thought is marked "do you stand behind it?".
  - Nothing is accepted. A preservation report gives the words before and after, tables, numbers, terms, what was
    held back, and what wasn't touched (headings, kept tables, paragraphs with notes, fields, pictures or pending
    changes).

## 2026-10-22 — Layout fixes are the person's decision

Owner's principle: editorial intelligence stays within limits. The system asks, and the person decides and takes
responsibility, so nothing is fully automatic. Sometimes the person follows an instruction ("the boss wants it this
way") instead of the logical layout, and the system accepts that.
- **`validate_document(fix=true)` only proposes:** `proposals` [{id F1…, issue, object, page, change}], and nothing
  changes.
- **`fix=["F1", …] | "all"`** applies only those, giving a new version that is laid out again. If something still
  needs a stronger step (a page break), that comes back as a proposal and is never taken without asking.
- **`keep=["E2", …]` + `reason`:** the problem stays as it is on purpose.
  - The decision is stored on the file, and later versions inherit it through the source chain.
  - It is listed under `kept_as_is` and never "fixed" back: not by the validator, the after-edit check or the fixer.
- **Settings made on purpose:**
  - A heading whose keep-with-next was switched off in the file is treated as intended.
  - When an edit explicitly sets `keepNext: false`, `pageBreakBefore: false`, `allowBreak: true` or
    `headerRow: false`, the after-edit check reports the result "as asked", not as a problem.

## 2026-10-22 — Document editor: background edits, real change detection, layout fixing

From an MCP agent's test report on a long study.
- **`edit_with_instruction` runs in the background.**
  - The answer comes within ~40 s. A longer edit returns `{status: "running", job_id, stage}`, and you ask again with
    `edit_with_instruction(job_id=…, wait=30)`.
  - The new version is named and checked even if the client stops asking.
  - Failures say what happened and that nothing was changed: the AI plan was too long (cut off), the planner didn't
    answer in 6 minutes, or the service failed. Each one suggests splitting the instruction or using wordcraft.
  - The plan may be up to 12,000 tokens (it was 4,096). A cut-off answer (`finish_reason = length`) is now a clear
    error instead of a broken JSON followed by a silent second model call. Failed operations are listed.
- **"Nothing changed" — and lost editor saves — fixed.**
  - The DOCX fingerprint now also sees: footnotes and endnotes, comments, tracked insertions and deletions, page and
    line breaks, fields, the pagination and spacing settings of paragraphs (keep with next, page break before…),
    numbering, headers and footers, section settings, and the visible properties of the styles in use.
  - Before this, a real footnote or a keep-with-next change was reported as "Nothing changed". Worse, the browser
    editor treated such a save as unchanged and **discarded it**.
  - A plain re-save (python-docx, WordCraft) is still "the same".
- **Leftover markers:** `[[N7]]`-style double-bracket markers count as placeholders. Every edit's check names new
  placeholders anywhere in the document, including footnotes, endnotes, headers and footers.
- **Paragraphs vs line breaks in wordcraft:**
  - `text.paragraphs {"text": "a\nb"}` (or `{"paragraphs": [...]}`) inserts real paragraphs.
  - In `text.insert`, `\n` is a line break inside the paragraph, as documented now; `\r\n` no longer leaves a stray
    line break.
  - `select.range` offsets are documented as UTF-8 bytes (á, ő = 2), with `select.text` recommended.
- **`validate_document(fix=true)`: layout fixing with re-checking.**
  - It lays the document out in both views, fixes what is safe, and lays it out again, for up to 3 rounds.
  - Fixed problems: headings at a page bottom (keep with next; a page break if a view still leaves them, so the
    editor and Word agree), captions apart from their picture or table, table rows breaking across pages, and table
    header rows not repeating.
  - The result is a new version, with `fixed` and the pictures and issues of what is left. The after-edit check points
    to it.

## 2026-10-22 — Background removal for video (green / blue screen), see-through avatars

- **`video(action="cutout")`** (also `POST /api/video/cutout`): a video filmed in front of a green or blue screen,
  without its background. No AI, no download, no cost: ffmpeg chromakey + despill.
  - The screen is found from the frame edges (or `key: green | blue | #RRGGBB`). How strong the keying is comes from
    measuring the edge pixels' UV distance from the screen colour.
  - `ai.background`: a colour, a picture (file_id, URL, `{"stock"}`) or a video (file_id, `{"stock_video"}`) gives an
    MP4. Left out, you get a see-through WebM VP9 (`alpha: "mov"` = ProRes 4444 ≤ 2 min, `"both"`).
  - The check says how much of a frame became see-through. Without a screen it says that removing a room behind
    someone needs AI matting, which isn't switched on.
- **Transparent cut-ins:** a see-through video used in a graphic or a `pip` keeps its alpha (cut to WebM VP9 alpha
  instead of H.264), and the pip shows it without its frame and uncropped. A cut-out presenter can stand in a corner
  of the footage.
- **Talking avatar without its background:** `ai.background = "transparent" | "#RRGGBB" | a picture / video`.
  - The photo's background is removed here (BiRefNet), the person goes in front of chroma green, and Kling animates
    that.
  - The result is keyed: a see-through .webm, or the avatar on the new background. The green-screen MP4 is kept too.
  - `ai.drop = ["floor lamp"]` takes thin things the cut-out counted as the subject.
  - A transparent avatar can't go straight onto a timeline; use a pip layer.

## 2026-10-22 — Social posts for a video

From the look at MoneyPrinterTurbo.
- **`video(action="social")`** (also `POST /api/video/social`): the posts that go with a video.
  - Input: a video, its project, or a sound file.
  - Output per platform: a title (YouTube), the text and hashtags, in each platform's own style and limits — YouTube,
    TikTok and Instagram by default; Facebook, LinkedIn, X or "all" on request.
  - It also gives YouTube chapters, keywords and a .md file to copy from.
  - Data first: the project's script and timeline, an overlay's subtitles and chapter / name layers, or a recording's
    transcript.
  - Then deterministic: chapters follow YouTube's rules (from 00:00, at least 3, each ≥ 10 s, a short title card
    gives way to the first scene); keywords by frequency without filler words; picture / footage credits go into the
    longer texts.
  - Then one Studio AI step writes the texts in the video's language from its facts only. Numbers that aren't in the
    video come back in the check, and the same material is answered from the cache.
  - Without the AI: plain drafts (title, first sentences, keyword hashtags), and the check says so.
  - `text` = notes to work in (a link, a call to action); `ai.fresh` writes them again.

## 2026-10-22 — A/B variants

From the look at MoneyPrinterTurbo.
- **`video(action="variants")`** (also `POST /api/video/variants`): 1–3 versions of a finished video for testing on
  social media; the original is variant A.
  - `changes={"variants": [{…}, {…}]}`: one set of changes per variant, anything `edit` takes. For example a
    different opening title or first scene, music, transition, accent, caption style or end title.
  - `changes={"count": n}`: the look varies without AI or cost. Made videos change the transition, overlays the
    caption style, and both change the accent; library music changes mood, while your own music stays.
  - The pictures and sound are reused, so no new AI pictures are made unless a variant asks for one.
  - Each variant is its own project and MP4, named "… – variant B".
- **Fixed:** an overlay edit refused `caption_style`.
- **Fixed:** an overlay project didn't store its caption style, so a later edit turned `bold` captions back into `box`.

## 2026-10-22 — Captions from the script, pauses in the narration

From the look at MoneyPrinterTurbo.
- **Captions follow the script:** when the spoken text is known, the captions take its words and only the timing
  comes from the listening.
  - Pass it as `transcript={"script": "…"}` (overlay, audiogram), `text=` (`action="transcript"`, audiogram) or in
    the web subtitler's new field.
  - Matching words keep their heard times. Misheard words are replaced by the script's (names spelt as written), and
    1–3 unheard words are fitted in between.
  - Longer unscripted speech is kept, a short "öö" is dropped, and a script start or end that isn't said is left out.
    Every change is in `corrections` (`by: "script"`).
  - If fewer than 40% of the script's words are heard, it isn't for this recording; the captions stay as heard and
    the check says so.
  - A talking avatar made from text remembers its text, so its captions (overlay, audiogram, transcript) follow it
    without asking.
- **`[pause: 1.5s]`** (also `[pause 500ms]`, `[szünet: 1 mp]`, ≤ 10 s) in a caption or an avatar's text: the voice
  stops there.
  - The mark is never shown on screen, and a pause at the very end doesn't lengthen (or raise the price of) the speech.
  - With `keep_wording`, the scene writer keeps the marks.

## 2026-10-22 — Waveform and audiogram

From the look at vanta: audio visualisation for podcasts and interviews.
- **`waveform` layer:** `{"type": "waveform", "style": "bars" | "rise", "bars"?: 8–64, "color"?, "position"?}`.
  - The footage's sound as moving bars: the loudness is measured every 1/15 s (fast attack, slower release) and put on
    a slider; each bar follows it through an expression, with its own height and a little life.
  - A part without sound leaves the bars flat, and the check says so.
- **`video(action="audiogram")`:** a podcast / interview / voice note as a video.
  - Input: a sound, or a video's sound, ≤ 30 min.
  - The cover is fitted over its own blurred copy; without one, a dark accent background.
  - It gets a waveform, the title and automatic word-by-word captions (`bold` by default).
  - The result is an editable overlay project, so the layers can be changed with `edit`. Also `POST /api/video/audiogram`.

## 2026-10-22 — Talking avatar

From the look at vanta: a talking video from one photo.
- **`video(action="avatar", ai={…})`** (Kling AI Avatar via kie.ai, lip-synced). The `ai` fields:
  - `image`: a photo of the face;
  - `audio`: a recording, ≤ 5 min (a video's sound works too), OR `text`: read out by the built-in Piper voice
    (`voice`, `language`);
  - `quality`: 720p (default) | 1080p;
  - `prompt`: expression and camera;
  - `consent: true` — required: the person in the photo agreed.
- **Placement** like an AI clip (`at` / `after_scene`) or a file of its own. The result's sound is levelled like the clips.
- **Price:** the seconds of speech × 8 (720p) / 16 (1080p) kie credits, through the common rule. Paid credits only
  (`avatar` joins `PAID_ONLY_OPS`). `action="estimate"` with `ai={"for": "avatar", …}` gives the price.
- **No AI label in the files** (owner, 10-22: "vedd ki az ai jelölést, majd a legvégén lehet, hogy betesszük" — telling
  viewers it's AI is the user's call). The AI clips' post-processing only levels the sound. The XMP label on AI pictures,
  edits and restorations is off too; `AI_LABEL=1` turns it back on.

## 2026-10-22 — Scene transitions

From the same look at vanta / MoneyPrinterTurbo: we only had a 0.5 s crossfade between scenes.
- **`transition`** on make (and `changes.transition` in an edit), or a scene's own `"transition"` for how it comes in:
  - motion: `fade` (default) | `dip` (through black) | `slide` | `slide-up` | `push` | `push-up` | `zoom`;
  - EffectCraft's transition effects: `wipe` | `wipe-up` | `clock` | `iris` | `blinds` | `blocks` | `cards` (3D
    flipping cards) | `grid` | `jaws` | `light` (a glowing ring) | `twister` | `sweep`;
  - `mix`: a different one at each cut.
- **How it is drawn:** the effects run "Transition Completion" 100 → 0 over the cut. An effect the engine refuses
  falls back to a fade.

## 2026-10-22 — Word-by-word captions

From the owner's look at vanta and MoneyPrinterTurbo: the short-video caption styles we didn't have.
- **`caption_style`** on overlay (and in an edit's `changes`):
  - `box` (default, as before);
  - `karaoke`: the spoken words light up;
  - `highlight`: the word being said lights up and lifts;
  - `reveal`: the words appear as they are said;
  - `bold`: no bar, big outlined capitals, 1–3 words at a time (a new group at a comma or the end of a sentence), the
    word being said in the accent colour.
- **Word times:** with `subtitles="auto"` they come from Whisper's word timestamps (`transcript.cues` now carries one
  start per text token; punctuation glued to a word is not a token). Own subtitles may give `words` (one start per
  word); otherwise the times are estimated from the letters.
- **Engine:** one text animator per caption: fill colour / position lift / opacity, with a word-based range selector
  keyed at each word's start (hold). The words lift instead of growing: a scale-up runs into the next word in this engine.

## 2026-10-22 — Free real footage for cut-ins (Pexels)

Owner's question: can we do free cut-ins with real footage?
- **`video(action="stock", text="words")`** → free stock video candidates from Pexels, with a numbered picture of their
  first frames so the connected AI sees them. Each has its title, length, size and the videographer
  ("Video: Name / Pexels"); the result carries "Videos provided by Pexels", as Pexels' API guidelines ask.
- **Any cut-in layer takes it**:
  - in `fullscreen`, `split` and `pip`: `image={"stock_video": "pexels:<id>"}`, a Pexels link, or
    `{"stock_video": "words"}` (the first that fits the box);
  - the file is chosen for the box: landscape / portrait / square, the smallest with the short side ≥ the box (720–1080 px);
  - it is downloaded into the person's workspace with its licence, source and credit in the file's details.
  The videographer is credited on screen and on the end card ("Footage: Name / Pexels").
- **Licence** (pexels.com/license, checked 2026-10-22):
  - free to use and edit, also in ads; attribution isn't required but we always give it;
  - not allowed: identifiable people in a bad light, selling unaltered copies, redistributing on other stock sites,
    implying endorsement. The `how` text says so.
- `PEXELS_API_KEY` (env). Searches are cached for an hour (Pexels: 200 requests an hour, 20 000 a month).

## 2026-10-22 — Even sound, picture in picture

From the Wan version of the "Reggeli különkiadás" product test.
- **Sound levelling** (`app/tools/loudness.py`, EBU R128 two-pass `loudnorm`, linear, target −16 LUFS / −1.5 dBTP):
  - **merge-files**: every part comes out at the same loudness. The Wan clips' generated sound was ~13 dB louder than
    the speech footage, so the volume jumped at each clip. On the test video the parts now sit within 2 dB, where
    before they spread from −16 to −39 dB mean volume. On by default for videos; `level_audio: false` keeps the
    original levels, and audio merges can turn it on. A near-silent part is never boosted.
  - **A part without sound no longer mutes the merge.** Before, one silent clip (an AI clip without audio) made the
    whole merged video silent; now that part gets silence.
  - **AI clips** are levelled when they arrive.
  - **Cut-ins** (fullscreen / split / pip with sound) are levelled to the footage's own loudness, kept within −28…−12 LUFS.
- **Picture in picture** — `{"type": "pip", "image": <video or picture>, "corner": "top-right" | …, "size": 0.2–0.7,
  "aspect": "16:9", "sound": "off" | "mix" | "replace", "clip"?}`: the footage stays full screen and the other video
  plays in a framed window in a corner, sized for the frame. It is the vertical-video answer to `split`, which shrinks
  a vertical base into a small window.
- **Engine fix: a `pop` / `grow` / `pulse` animation no longer resets a fitted picture or video to its native size**
  (the builder keyframed scale to 100%). A 1280×720 video in a 648-px window came out at 1280 px wide, covering its own
  frame.
- **All-caps text is measured wider** (capitals ~0.7 em, narrow letters ~0.32, the rest ~0.56, instead of 0.56 for
  every letter). An all-caps lower-third name was counted as one line, broke into two on screen and ran over the role
  line. Now it is set smaller and fits.

## 2026-10-21 — Wan 3.0 instead of Seedance, AI pictures on kie.ai, prices that can't lose money

Owner's decisions: Seedance goes ("kidobjuk a seedance-t. Wan lesz"); 720p is the default; the person can choose the AI
engine; no loss in any case; pictures come from kie.ai too ("nem akarom keverni").
- **AI clips: Wan 3.0** (`wan/3-0-video`, the default) or **Wan 3.0 Fast** (`wan/3-0-video-prime`, kie's high-speed
  version, dearer). `engine` on `ai_clip` and an "AI engine" picker in the web form; resolution 480p | 720p (default) |
  1080p; 2–30 s; aspect 16:9 | 9:16 | 1:1 | 4:3 | 3:4 | adaptive. Text, first (+last) frame and reference mode as before;
  Wan's limits: ≤ 10 reference pictures, ≤ 5 reference videos / sounds, 1–15 s each and ≤ 15 s in total, reference video
  + clip ≤ 30 s (billed seconds = clip + reference video). The prompt calls the references Image1, Video1, Audio1….
  kie's content filter (`nsfw_checker`) is on. Same price with or without sound.
- **Seedance is gone**: the models, prices, the draft → final flow (`draft`, `finalize`, `/api/ai/draft-of`, the "Make
  final" button). A `draft` / `finalize` request gets a clear message; the old Seedance price rows are retired on start.
- **One pricing rule for every external AI cost** (`app/credits.py`): credits = provider cost × 2 ÷ the least we net on
  a credit we sell. That floor is computed from the plans and packs (`credit_floor_eur`): 27% VAT (the highest in the EU),
  the dearest card fee (3.25% + €0.25), and the yearly plans' 12 monthly allowances. Today that's Pro yearly,
  €0.0298 a credit. Dollars count at €0.95 (ECB today: 0.89). So even the cheapest credit buys AI at twice its cost.
  The rule follows a change of plan or pack prices by itself.
  - A price row can be raised in the admin but not saved below the floor, and `price()` lifts a row below it anyway.
    A row without a provider is recognised from its model. On start, rows below the floor are raised; an admin's
    higher price stays.
  - Today: a 5 s clip is 13 / 26 / 51 credits (480p / 720p / 1080p) on Wan 3.0 and 20 / 41 / 81 on Wan 3.0 Fast. Topaz
    on kie is 8 credits per 3 s at 1–2×.
  - Machine invariant: `tests/test_pricing_floor.py`.
- **AI video runs on paid credits only.** The free monthly and the welcome credits have no revenue behind them. Paid means
  credited from Stripe (pack or subscription): the sum of those, minus everything ever reserved or charged for
  `ai_clip` / `enhance`, checked under the account's row lock. Otherwise `paid_credits_only` (MCP and HTTP 402), with how
  much is needed and how much is left.
- **The provider charging more than we reserved is reported** (admin notice `ai_cost_above_hold` /
  `image_price_below_cost`). The charge still stops at the reservation.
- **AI pictures on kie.ai, the same models**:
  - FLUX.2 [pro] generates: 1K up to ~1 MP, 2K above. Slide and video-scene pictures (1792×1024…) are 2K, never smaller
    than asked.
  - FLUX.1 Kontext [pro] edits: restoration, colourising, retouch, AI text removal. The original's ratio is kept.
  - SiliconFlow no longer makes pictures. Its language and vision models stay.
  - Prices from the same rule: 2 credits (≤ 1 MP), 3 (larger), 2 per Kontext edit. A restoration is 2, +2 to colourise
    (was 2 and +1).
  - Every picture goes through the ledger: reserve → charge with kie's real cost (`provider_cost`), so the daily
    reconciliation sees it and admin use is logged. A failure costs nothing.
  - Pictures can still use free credits.
  - `IMAGE_CREDIT_COST` is gone; the account carries `image_credit_cost` (a framed picture) and `edit_credit_cost`.
- **Pricing page and Plans** list every AI price: picture, larger picture, edit, restore and colourise, and a 5 s clip
  per engine and resolution. They also explain the rule and the paid-credits-only rule.
- Needs `KIE_API_KEY` in production for pictures (as for AI video). The `AI_VIDEO` switch still gates video only.
- **Yearly plans get their credits every month.** Until now a yearly subscriber got one month's credits per invoice,
  which is once a year (400 on Pro yearly instead of 12 × 400). Now the billing cycle is recorded (`users.billing_interval`:
  at checkout and from the invoice). The yearly plan's monthly credits come in every month while the subscription lives:
  at the invoice, then from a background loop every 6 hours and when the account is opened. The key is idempotent per
  month (`stripe:yearly:<user>:<YYYY-MM>`), so the invoice and the monthly grant never both pay. They are paid credits.
  Monthly plans are unchanged (one grant per invoice).

## 2026-10-19 — Own music on own footage

From the owner's product test ("Tamás TV – Reggeli különkiadás"). Music is the person's own; what we offer ourselves has
to be right ("korrekt"), and quality never goes down.
- **Own music on an overlay (own footage).** Until now an overlay only took the built-in library and ignored
  `music_file_id`. Now `music={"file_id", "start"?, "level"?}` (or `music_file_id`) works on `overlay` and in its edits:
  - `start`: where the song begins (seconds or "MM:SS");
  - `level`: `background` (default, 14 dB under the footage's speech) | `even` (6 dB under it) | `front` (the song as it
    is, the footage's sound 14 dB lower) | `only` (the footage's sound muted);
  - the song is kept lossless (WAV) until the single final encode and isn't looped (a shorter song ends, then silence);
  - the project keeps its own copy of the song, so an edit (`changes.music = {"level": "front"}`) still works after the
    upload expired.
  `make` takes the same object (file or mood); `start` / `level` there are refused with a pointer to `overlay`.
- **Built-in music library off by default** (`MUSIC_LIBRARY=1` turns it on). Its 24 loops are CC0 but synthesised, not
  the quality we put our name on. A mood request without the library gives a video without music and a check finding
  that says to ask for the person's own. The web form's mood picker is gone; the music slot takes their own file.
- **Video audio is AAC 256k** (was 192k; `VIDEO_AUDIO_BITRATE`), so a song the person brings doesn't lose quality.
  An enhanced video keeps its original sound as it is (copied, not re-encoded).

## 2026-10-19 — Enhance through fal.ai (no GPU on our server)

kie's Topaz failed with failCode 500 on every upscale all day on 2026-10-19, kie's own sample included, and kie has no
other upscaler for arbitrary video. The server has no GPU, so the enhancer stays a hosted service: a second provider
behind the same interface (`app/providers/fal.py`).
- **fal.ai provider**: queue API (submit → status → result, cancel), upload to fal's CDN (fal's official client's
  protocol), the result downloaded to our storage at once. fal returns no per-job cost, so the charge is the estimate from
  its price list; it has no balance API, so the daily reconciliation skips it.
- **Engines** (`ENHANCE_ENGINE`): `seedvr` (SeedVR2, the default when `FAL_KEY` is set — the reference for old, small,
  compressed footage), `topaz-starlight`, `topaz-proteus` (Topaz on fal) and `topaz` (kie, the old path and the default
  without `FAL_KEY`).
- **Prices** from fal's pages (2026-10-19), same Studio credits per dollar as kie: SeedVR2 $0.001 per output megapixel
  (1080×1920 @30 ≈ $0.062/s); Topaz $0.01 / $0.02 / $0.08 a second for output ≤720p / ≤1080p / above. The Starlight
  price isn't listed separately — check it before `AI_VIDEO=1`.
- `FAL_KEY` env only; without it nothing changes.

## 2026-10-19 — OPERATION OMLETT

Studio development package from the 2026-10-19 live MCP test (the "omlett" news video) and the owner's requests.
Five lanes, merged on `feat/omlett`. Architecture notes: `docs/ARCHITECTURE-studio.md`.

**Off by default / needs env:** sharing (`SHARING_ENABLED=1`), AI video via kie.ai (`KIE_API_KEY` + `AI_VIDEO=admin|1`).
**Behaviour changes:** merge-files fills a clip of another shape with its blurred copy (was black bars) and keeps a common
fps; tool outputs use CRF 17 / AAC 192k (bigger files on grainy footage); the final render goes through a temporary ProRes
master + x264 CRF 19; downloads/exports drop GPS/location metadata (`STRIP_LOCATION=0` keeps it).
**Deferred:** AI Act / C2PA marking of AI clips (legal, out of scope for now); re-rendering only the changed section.

### Phase 0 recon + 3.5 Hungarian transcript

#### 0 — Recon
- `docs/ARCHITECTURE-studio.md`: roles, credits and metering per path, MCP registration and auth → uid, storage /
  versions / signed links / retention, video pipeline and its check, transcript, sharing — each with `file:function`.

#### 3.5 — Hungarian transcript
- New transcript parameters, the same on MCP `video(action="transcript")`, REST `POST /api/video/transcript` and the
  web subtitle maker ("Better recognition"):
  - `prompt` → Whisper `initial_prompt` (what the recording is about);
  - `vocabulary` (list or comma-separated) → Whisper `hotwords`, then the vocabulary fixes;
  - `model`: `large-v3-turbo` (default, `ASR_MODEL`) | `large-v3` (one model in memory at a time);
  - `force`: listen again, bypassing the cache; the new result replaces the cached one;
  - `correct`: left out = vocabulary fixes only (no AI) · `true` = the Studio AI proofreads what is still misheard ·
    `false` = no corrections.
- Cache key (Postgres `ocr_cache`) = audio sha256 + language + a hash of prompt/vocabulary + model; calls without the
  new parameters keep the old key, so earlier transcripts stay hits.
- Corrections, in the owner's order (data → deterministic → AI): (1) vocabulary fixes without AI — a misheard word
  with the same first letter, ≥ 5 letters and a similar sound is replaced, but never another form of the same word
  ("fakanál" stays next to "fakanállal") or a different word ("Kanállal"); (2) with `correct=true` the Studio AI gets
  the remaining text (one AI step per ~6000 characters through `_call_model` → `billing.ai_step`: on MCP the monthly
  allowance, then credits; on the web fair use as everywhere). AI fixes are applied only where the misheard words
  stand verbatim in that sentence and nothing is rephrased; AI decisions are kept in `content_checks` (same text +
  vocabulary → no second charge). Out of AI steps: the transcript still comes back, with the reason in the check.
- Results carry `corrections [{at, heard, fixed, by: vocabulary|ai}]`, `model` and, when the AI worked,
  `studio_ai {steps, credits}`; subtitles and the `.srt` are built from the corrected words.
- Fixture (10-19 mishearings, correct → heard): sonkával → sokkával, fakanállal → fokonállal, Jöhet a tálalás →
  Jó natállalás are fixed from the vocabulary; szép → kép, azt ajánlom → azt jön, Voilà → Ala by the (mocked) AI.
- `transcript.SCHEMA_REF["transcript"]`: the parameter reference for `video(action="schema")` (lane "video" wires it).
- Measured on real large-v3-turbo (CPU, synthetic Piper speech of the fixture sentences, ~12 s): without parameters
  it heard "ometet", "somkában", "Vojra"; with `vocabulary` + `prompt` "omlettet" and "sonkával" came out right
  ("Voilà" is left to `correct=true`). That run also caught a false vocabulary fix ("azt ajánlom" heard right →
  "azt azt ajánlom"); exact hits of a vocabulary phrase are now left alone (regression test).
- Tests: `tests/test_transcript_hu.py` (10 tests fail on main and pass here; + an opt-in real-Whisper test,
  `ASR_SLOW_TEST=1`, verified with the tiny model).

### Video tool — 1.3, 1.4, 3.4 end card, 3.6, 3.7, phase 5

#### 1.3 — the video check sees text collisions
- A graphic's `text` without `w` no longer wraps at a hidden 0.6 of the frame: it stays on one line while it fits in
  0.92 of the frame width (measured with Inter, the renderer's font — ő/ű included; short texts keep their 0.6 box, so
  older videos look the same; left/right-aligned text keeps its old anchor edge).
- The builder reports every graphic text's drawn (ink) box from the engine (`sourceRectAtTime`); the check turns them into
  findings: two texts overlapping (same layer or two layers on screen together), a text running out of its enclosing
  `rect` or off the frame, an unexpected line break (no `w`). Each finding names the layer/scene, the element and a
  suggested `w` / `size` (or `y`). Status becomes `needs_check`.
- `render=false` (overlay / make / edit) still returns this layout check.
- Measured on the reconstructed 10-19 layers (`rendkivuli`, `tudta`, `hir3`, main's 0.6 box): main 0 findings / `ok`,
  now 3 named findings / `needs_check`. Without `w` the new default keeps "RENDKÍVÜLI KIADÁS" (66 px, ~663 px wide) and
  the Tudta line on one line; only the over-long ticker text is still reported (with a size that fits).

#### 1.4 — short `video` tool description + `action="schema"`
- The `video` MCP description went from 21 902 to 1 945 characters (cap 2 000, tested; sent without indentation). It
  lists the main actions and points to `video(action="schema", topic=…)`.
- New `action="schema"`: topics make | overlay | layers | graphic | chart | subtitles | edit (no topic = the list). The
  registry is a plain dict, `app.tools.video_schema.TOPICS` — another lane adds a topic with
  `TOPICS["adjust"] = video_fx.SCHEMA` (no other wiring needed).
- A layer with a `chart` object and no `type` is a chart layer (a list of `elements` without `type` is a graphic); the
  type / `at` / chart errors carry a minimal example and point to the schema.
- New MCP params: `topic`, `render` (`render=false` for make / overlay / edit: save the project only).
- Other tool descriptions over 2 000 characters (not changed here): wordcraft 4 829, design 4 785, pptx_build 4 246,
  forms 3 698, file_tools 3 046, office 2 901, validate_document 2 531, files 2 297, convert 2 255, facts 2 080.

#### 3.6 — `move_to` timing
- The glide starts once the element has appeared (after the ≤ 0.5 s fade-in for `appear`, after `dur` for pop / grow /
  slide, 0.3 s for pulse) and lasts `move_dur` (new; default `dur`), eased. It starts from the element's own place
  (a slide-in no longer makes it start from the slide offset).
- Regression (rendered frames, marker at x 0.2, `at` 1.5, `dur` 2.2, `move_to` [0.88, y]): at 4.3 s main had it at
  x ≈ 0.2 (the move started at 3.9 s and lasted 4.4 s); now x = 0.88 (arrives at 4.2 s).

#### 3.7 — Lottie search
- `{"search": …}` now loads up to five candidates and orders them: transparent background first (a solid layer or a
  filled rect covering the canvas counts as opaque), then the shape that fits the element's box, then the vision
  ranking. `"pick": n` takes the n-th of that order.
- The result has `lottie_picks`: where it went, name, author, `thumb_url` (preview), `gif_url`, page, its pick / of,
  transparent, size, and the alternatives with their previews. The chosen URL, name and preview are kept in the spec.
- The check samples every Lottie element (its comp rendered transparent at 25/50/75 % of its loop): an animation that
  fills its whole box (opaque background) or sits on a measured text is a named finding; a clean one is in `verified`.
- Tests mock LottieFiles (no network); the sampling test renders synthetic Lottie JSONs with the engine.

#### 3.4 (overlay side) — end card that extends the footage
- `video(action="overlay", …, end_card={"title", "subtitle"?, "kicker"?, "dur"?})` (or `end_title` / `end_subtitle`):
  the footage is extended by `dur` seconds (default 4) — last frame frozen with `tpad=stop_mode=clone`, sound padded
  with `apad=pad_dur` (one x264 CRF 17 pass, a hidden work file) — and a full-frame card (80 % veil over the frozen
  frame, kicker / title / accent bar / subtitle, the picture and animation credits) sits on the added seconds.
  An overlay may have only an end card.
- Edits: `changes.end_card` changes only the given fields (a new length re-extends from the plain footage; the same
  length reuses the extension); `"none"` removes it and the video is back to the footage's length.
- Measured: 3.0 s clip + `dur: 2` → 5.0 s video, audio 5.0 s; frames at 3.6 s and 4.6 s are identical under the card.
- The general extend-video / image-to-video operations belong to lane "media".

#### Cross-lane hooks (schema registry, transcript hints)
- `video_schema.register({topic: text})` adds any lane's reference; `app.tools.transcript.SCHEMA_REF` (lane
  transcript) is picked up automatically when present, so `video(action="schema", topic="transcript")` works after the
  merge without wiring.
- Overlay `transcript={"prompt", "vocabulary", "model"}` (MCP param `transcript`) is passed to the speech recognition
  behind `subtitles="auto"` and `at: {"say": …}` (also kept for edits) — only the keys `transcript.transcribe` accepts,
  so it is a no-op until lane transcript's new parameters are merged.

#### Phase 5 — video tool details
- `make`: new `scrim` (0–1, also per scene; `0` = no darkening) controls the veil over title card, scenes, chart / number
  scenes and end card — `backdrop: 0` alone left a 70 % shade before. The title card's kicker gets a dark pill sized to
  its rendered text (engine `sourceRectAtTime`) over a photo, so the red accent is readable.
- Fixed length compensates the crossfades: when every scene has its own `duration` and they come out short, they are
  stretched to the asked length (main: 10 s asked, 3 × 2.5 s scenes → 8.9 s; now 10.0 s). Scene lengths stay minimums
  (too long is still reported, never cut).
- `opacity` is 0–1 everywhere and stored that way (0.94 was stored as 94.0); 0–100 is read as percent; old saved specs
  keep working. Diagram graphics now emit 0–1 too. Documented in the `graphic` schema topic.
- Layer listings / comp names: a graphic is labelled by its title or first text (`graphic “RENDKÍVÜLI KIADÁS”`), else
  `graphic (3 elements)` — no more `graphic “”`.
- `video(action="status", job_id, wait=30)` waits up to 30 s (≤ 55) while the job runs. The `jobs` MCP tool now finds
  Studio jobs (video, render, transcript) too, with a note, instead of "No such job.".
- Faster turns: `render=false` (save only, layout check still returned), `draft=true` on make / overlay / edit (half size,
  2.5 Mbps draft; previews use it until a full render; `info.last_render.draft`). Re-rendering only the changed section
  into the existing MP4 is NOT done: the engine renders spans, but splicing separately encoded H.264 pieces without
  re-encoding needs keyframe-aligned cuts and identical encoder parameters, which the CLI doesn't expose — the existing
  `action="render", scene="L2"` clip covers checking one part.
- Credits: "Pictures and animations:" whenever a Lottie is in the video (main only looked for a credit starting with
  "Animation ", so a Lottie with its own credit got "Pictures:").
- A graphic image / Lottie / cut-in that couldn't be loaded is now a named finding (it was only logged; the check said
  `ok`).

### Media ops — 1.1, 1.2, 3.2, 3.3, 3.4 ops, phase 5 info/metadata

#### 1.1 merge-files keeps portrait clips portrait
- The canvas comes from the first clip's DISPLAY size (after rotation: displaymatrix side data and the legacy `rotate`
  tag are both read) — 2 portrait iPhone-style clips (coded 1920×1080, rotation 90) + 1 landscape → 1080×1920
  (it was 1920×1080 with the portrait clips in a ~608 px strip).
- New merge-files options: `canvas` = auto | vertical | landscape | square | WxH, `fit` = blur (default: a clip of
  another shape sits on its own blurred copy) | contain (black bars) | cover (fill, edges cut).
- resize-video: `aspect_mode = blur` (same helper).
- Shared modules: `app/tools/mediaprobe.py` (duration, display size, rotation, fps, codecs, audio, HDR, bitrate) and
  `app/tools/videonorm.py` (canvas size, fit chains).

#### 1.2 One encoding profile (generation loss)
- `app/tools/encoding.py`: intermediate x264 CRF 17 (tool ops: resize, merge, trim, crop, rotate, watermark,
  gif-to-video, footage set upright, cut-ins), final x264 CRF 19, preset medium, AAC 192k, +faststart. Env:
  `VIDEO_CRF_INTERMEDIATE`, `VIDEO_CRF_FINAL`, `VIDEO_X264_PRESET`, `VIDEO_AUDIO_BITRATE`, `VIDEO_FINAL_ENCODER`.
- Final render: the engine's own H.264 only does a fixed bitrate (8 Mbps; its "quality" mode is HEVC/AV1 only), so
  the engine now writes a ProRes LT master (~100 Mbps, temp, deleted) and x264 CRF 19 makes the MP4 (tagged BT.709).
  `VIDEO_FINAL_ENCODER=engine` restores the old path; draft renders stay on the engine's fast H.264.
- merge-files was already one pass (scale + concat in one filtergraph); it stays one pass.
- Omlett chain, measured (synthetic 1080×1920 30 fps testsrc2 + blur + temporal grain, ~12.6 Mbps x264 source,
  4 s; resize-video → merge-files → video overlay; SSIM/PSNR vs the source over 114 frames, ffmpeg ssim/psnr):

  | stage | before (main a9abf31) | after |
  |---|---|---|
  | resize-video | 6.58 Mbps · SSIM 0.98638 · PSNR 46.56 dB | 13.09 Mbps · SSIM 0.98979 · PSNR 48.56 dB |
  | merge-files | 6.54 Mbps · SSIM 0.98451 · PSNR 45.44 dB | 12.41 Mbps · SSIM 0.98723 · PSNR 47.21 dB |
  | final render | 8.36 Mbps · SSIM 0.97666 · PSNR 35.32 dB (Y: 0.98020 / 37.88) | 5.52 Mbps · SSIM 0.97781 · PSNR 35.29 dB (Y: 0.98104 / 37.73) |

  The final PSNR has a ~35 dB ceiling that no bitrate lifts: the engine resamples the footage (4:2:0 → RGB →
  4:2:0, different chroma interpolation at sharp colour edges) — even its ProRes HQ output or its H.264 at 20 Mbps
  stay at 34.4 dB on this content. The final step's gain is SSIM at a smaller file; removing the ceiling needs the
  graphics composited onto the original footage in ffmpeg (alpha render + overlay), a renderer change.
  Grainier source (testsrc2 + strong noise): final 8.31 → 11.34 Mbps, SSIM 0.97284 → 0.97486.

#### 3.3 HDR input (HLG / PQ)
- `color_transfer` arib-std-b67 (HLG, iPhone) or smpte2084 (PQ) → zscale to linear (npl 100) → BT.709 primaries →
  `tonemap` → BT.709 transfer, 8-bit 4:2:0, in the shared normalisation (`videonorm.head_filters`): merge-files,
  resize-video, the overlay's base footage (set upright / mapped once before the engine) and cut-in clips.
- Algorithm `VIDEO_TONEMAP` (default mobius; hable, reinhard, clip… allowed), `VIDEO_HDR_TONEMAP=0` turns it off.
  Measured on a synthetic 10-bit HEVC HLG clip (SDR testsrc2 → BT.2020/HLG, reference white 75%), signalstats vs the
  SDR original (YAVG 125 / SATAVG 112): untouched (main) 126 / 44 — saturation 39%, washed out, still tagged HLG;
  mobius 121.5 / 95.3 (97% / 85%); hable 101.3 / 77.8 (81% / 69%, darker) — hence mobius as the default.
- zimg/zscale is in the image's ffmpeg 4.4.2 (checked); the test skips where it isn't.

#### 3.4 Extend a clip, image → video, allowlist (ops part)
- ffmpeg-advanced allows `tpad`, `loop`, `apad` — bounded (≤ 3600 s padding, loop repeats × size ≤ 18 000 frames,
  no bare `apad`), so no request can produce an endless output. A refused filter or flag now lists what is allowed.
- New tool `extend-video` (options `duration`, `at`): holds the last frame for `duration` s (room for an end card),
  or freezes the frame at `at` for `duration` s and plays on; silence during the hold, sound stays in sync.
- New tool `image-to-video` (options `duration` default 5, `motion` still | zoom_in | zoom_out | pan_left | pan_right |
  pan_up | pan_down, `canvas`, `fit`, `fps`): a still (EXIF-rotated) → MP4 clip with an optional Ken Burns move
  (zoompan on a 2× upscale, no jitter) and a silent stereo track (so merge-files keeps the other clips' sound).
- Both are catalog tools: web page + REST (`tool/extend-video`, `tool/image-to-video`) + MCP `file_tools` + agent.

#### 3.2 Video effects (CPU, ffmpeg)
- `app/tools/video_fx.py`: stabilise (vidstabdetect + vidstabtransform, two passes; deshake where vidstab is
  missing — the image has vidstab), denoise (hqdn3d fast / nlmeans quality), sharpen (cas / unsharp), colour and light
  (eq brightness/contrast/gamma/saturation; curves for levels, shadows, highlights and highlight roll-off;
  colortemperature with preserved lightness; vibrance), LUT looks via lut3d from generated 17³ .cube tables
  ("Meleg konyha" warm_kitchen, "Hírstúdió" news_studio, "Mozi" cinema, "Fekete-fehér" black_white).
- Auto: signalstats on the keyframes (YAVG, YMIN/YMAX, YLOW/YHIGH, SATAVG) → black/white point, gamma, saturation and
  roll-off for hot highlights — bounded, only for visible faults; the user's own values win.
- Preview: 4 frames, left half before / right half after, as one PNG (before the full render).
- New tool `adjust-video` (web, REST, MCP file_tools; flat options auto, brightness, contrast, gamma, saturation,
  shadows, highlights, highlight_rolloff, temperature, vibrance, lut, denoise, sharpen, sharpen_method, stabilize,
  preview). Audio is copied (AAC), video at the intermediate profile.
- `video` edit: `changes.adjust` (same keys) corrects an overlay video's footage — always from the clean
  (upright / tone-mapped) original, `{}` resets; layers stay. A made (scene) video says to use adjust-video instead.
- Wording everywhere: clipped highlights can't be recovered, only toned down.
- Reference text for the video tool's `action="schema"`: `app.tools.video_fx.REFERENCE`.
- Measured on synthetic clips: shaky testsrc2 (±22 px jitter) frame-to-frame difference 43.5 → 14.9 with vidstab;
  a dim, flat clip gets levels 0.07–0.82 → 0–1 and gamma 1.2 from Auto.

#### Phase 5 (media items)
- `video info` on a raw video (not a project): duration, display width/height (after rotation), rotation, fps,
  video/audio codec, audio, HDR (hlg | pq), bit depth, bitrate, and the stored size when it is rotated. Info results
  (raw video and project) are read-only, so the automatic check no longer adds "It opens: 3 pages" — that line came
  from the generic check counting a video's 3 preview frames as pages; a video/audio result now says
  "It plays: 12.3 s, 1080×1920 at 30 fps, with sound." instead.
- `read-metadata` takes up to 100 files in one call: one JSON with `{count, items: [{file_id, label, …}]}`; one file
  answers as before. Media files also get a `video` summary (display size, rotation, HDR…) from mediaprobe.

### Credits + kie.ai — 1.5, 2.1–2.5

#### 2.3 Credit ledger (Postgres)
- `credit_ledger` extended in place (it already existed): `kind` (grant | purchase | hold | capture | release | refund |
  adjust | admin_usage), `workspace_id`, `operation`, `provider`, `provider_task_id`, `provider_cost`, `idempotency_key`
  (UNIQUE), `meta` (jsonb). Every balance change is one ledger row + `users.credits` (kept as the cached balance) in ONE
  transaction under `SELECT … FOR UPDATE` on the user row — `SUM(delta) = users.credits`.
- Migration keeps existing balances: old rows get a `kind` once; for every account whose balance doesn't add up to its
  ledger (credits granted by hand in SQL, the old 0-delta "signup" row) one `adjust` row "opening balance" with the
  difference, key `opening:<uid>` — idempotent on every restart; `users.credits` itself is never touched.
- `pricing_rules` (operation, model, tier, unit second|job|image, provider_cost, multiplier, min_charge): price =
  ceil(provider_cost × units × multiplier), at least min_charge; local CPU work has no rule → 0. Editable in the admin
  dashboard (💳 pill → modal).
- Paid external work: estimate → `hold` (max) → provider job → `release` + `capture` (actual, ≤ hold) in one
  transaction, or only `release` on failure. Keys `hold:/release:/capture:<op>` make webhook + polling races harmless.
- Daily reconciliation (lifespan loop, advisory lock): provider balance vs. previous snapshot − ledger provider costs →
  drift notice; balance under `KIE_LOW_BALANCE` (default 2000 kie credits ≈ $10) → low-balance notice; accounts whose
  balance ≠ ledger sum → notice. Notices = log warning + admin dashboard (no e-mail).
- UI: credit history on the Plans page (`GET /api/account/ledger`), `GET /api/estimate`, header balance (existing chip).

#### 1.5 MCP uses the same credit path as the web UI
- Every MCP tool call runs inside `credits.mcp_call` (in the `linked` wrapper): the token's user pays; an optional price
  per call comes from `pricing_rules` (`mcp:<tool>` or `mcp:<tool>.<action>`, default 0 — local CPU work stays free),
  held before the tool runs, captured on success, released on error.
- Not enough credits → the tool never starts; error text `insufficient_credits: this needs N credits, you have M …`.
- Admin is exempt but logged: one `admin_usage` row (delta 0) with `would_charge` / AI steps per paid call; paid
  provider jobs log their provider cost the same way.
- `studio_ai` result field extended (back-compatible: `steps`, `credits` unchanged) with `credits_used`,
  `credits_held` (provider job reservations), `balance`; admin: `admin: true`, `would_charge`.
- Code truth vs. the spec: MCP already charged credits for Studio AI steps over the monthly allowance and for paid
  work routed through the web handlers (digitize, restore, fact checks); what was missing was a per-call price hook,
  admin logging and the balance in the result.

#### 2.1 kie.ai adapter (swappable provider layer)
- `app/providers/base.py` (interface: `create(model, input) → task`, `status(task)`, `fetch_result(task) → stored
  file_ids`, `cancel(task)`, `balance()`, `upload(path)`), `app/providers/kie.py` (kie.ai), `app/providers/__init__.py`
  (registry + flag). Provider data (task ids, result URLs, field names) stays in the provider layer and
  `provider_tasks`; the video layer only sees files already in our own storage.
- Per-model field-name mapping table (`MODELS`): unknown fields are refused instead of sent; Seedance first/last-frame
  and reference modes can't be mixed; Topaz `upscale_factor` is a string.
- 429 → exponential backoff 1-2-4-8-16 s plus a client-side bucket (18 new tasks / 10 s, under kie's 20/10 s).
  kie returns errors as HTTP 200 with `code` in the body — always checked.
- Results are downloaded immediately (kie keeps generated files 14 days; uploads 24 h) through the shared SSRF-safe
  `net.download`.
- `POST /webhooks/kie?token=KIE_WEBHOOK_SECRET` (+ optional kie HMAC `KIE_WEBHOOK_HMAC_KEY`): the callback is only a
  signal — the result is re-read from `recordInfo` and matched to our `provider_tasks` row; idempotent (row lock +
  `settled` + ledger keys). No secret → no callback URL is sent, polling only (backoff 3 s → 30 s), and a resume loop
  finishes open tasks after a restart (a task stuck without a provider id for 10 min, or running > 6 h, is released).
- Feature flag: nothing runs without `KIE_API_KEY` AND `AI_VIDEO` = `admin` (admin only) | `1` (everyone).
- Live (2026-10-19): a request kie rejects at create time costs nothing; an accepted task that fails reports
  `creditsConsumed: 0` and the balance didn't move (4 failed Topaz tasks, 0 credits) → failed tasks are not billed.
  kie has no cancel endpoint: cancel is local (hold released; a late provider charge shows up in reconciliation).

#### 2.2 Models and prices (verified 2026-10-19 on kie.ai/pricing's own data source; 1 kie credit = $0.005)
| Operation | Model | Unit | kie credits | Studio credits @ ×0.15 |
|---|---|---|---|---|
| Seedance 2.5 480p / 720p / 1080p, no video input | `bytedance/seedance-2-5` | s | 28 / 63 / 158 | 4.2 / 9.45 / 23.7 per s |
| … with reference video ((ref + output) s) | same | s | 17 / 38 / 95 | |
| Seedance 2.5 draft (480p only) | same, `draft: true` | s | 28 (live: 4 s = 112) | 4 s → 17 |
| Draft → final 1080p | `bytedance/seedance-2-5-draft` | s | not published (held at 158) | |
| Topaz video upscale 1x–2x / 4x | `topaz/video-upscale` | s | 8 / 14 | |
| Topaz image upscale 2K / 4K (not wired) | `topaz/image-upscale` | image | 10 / 20 | |
- Seeded into `pricing_rules` only where missing (admin edits survive restarts). Default multiplier `0.15` Studio
  credits per kie credit (`KIE_CREDIT_MULTIPLIER`), `min_charge` 1 — owner decision, see the lane report.
- Estimates: `ai_video.estimate_clip` (resolution, 4–30 s, reference video seconds) and `estimate_enhance` (probed
  length rounded up × factor tier). Seedance modes (text | first frame | first+last | reference) can't be mixed.

#### 2.5 "Enhance" (Topaz video upscale)
- `video(action="enhance", file_id, factor=1|2|4)` over MCP, `POST /api/ai/enhance` + an "Enhance… AI" quick tool
  for videos and video projects in the app (only when AI video is on), with the estimate on the button
  ("Enhance — 4 credits"); `video(action="estimate", …)` / `POST /api/ai/estimate` give the price only.
- The result is a NEW version of the file (`source_file_id` = the source, label "… (enhanced 2x).mp4"), usable as the
  base of overlay / merge-files / edit; a project is enhanced from its last render.
- kie's API input limit is 50 MB (mp4/mov/mkv, same as the web form): bigger inputs are cut in time with stream copy
  (keyframe cuts, no re-encode; an oversized part is re-encoded to a target bitrate), upscaled part by part (one user
  operation, one hold, one capture) and joined back with stream copy; the original sound (first decodable track) is
  muxed back in.
- Credit pre-check before anything is uploaded: not enough → `insufficient_credits`, nothing starts.
- Live: kie's Topaz video upscale answered "internal error" (failCode 500) for every input on 2026-10-19 — also for
  kie's own documentation example — and billed 0; the flow is covered by tests with a fake provider that really
  upscales with ffmpeg.

#### 2.4 AI clip (Seedance 2.5)
- `video(action="ai_clip", ai={prompt, resolution, duration 4–30, audio, aspect, draft, image | first_frame (+ last_frame)
  | reference_images/videos/audios, at | after_scene}, file_id=<project>)`, `POST /api/ai/clip`, and an "AI clip… AI"
  quick tool on video projects and videos (estimate on the button; "Make final (1080p)" on a draft clip).
- Text→video; image→video from an uploaded picture or `{"frame": seconds}` of the current video/project (its last
  render) — the frame is prepared to kie's rules (JPEG, 300–6000 px, aspect 0.4–2.5); first/last-frame mode vs.
  reference mode are refused when mixed.
- Draft (480p, cheap) → `ai={"finalize": task_id}` → `bytedance/seedance-2-5-draft` with the draft's kie task id at
  1080p (kie keeps drafts 7 days); the final clip is a new version of the draft file.
- On the timeline (the existing edit path, a new project version): `at` → a full-screen cut-in layer (its sound mixed
  when the clip has sound); `after_scene` → a scene of its own (a graphic with the clip as a full-frame video element).
  If placing fails, the paid clip is still returned with the exact `changes` to place it by hand.
- The estimate is reserved inside the request (MCP `studio_ai.credits_held`), so `insufficient_credits` comes back
  before anything is uploaded or started.
- Live, through the full app path (ledger + kie, polling): 480p 4 s draft → kie charged 112 credits
  (`creditsConsumed`), the Studio captured 17, clip 854×480 24 fps H.264, downloaded from kie's CDN into our storage.
- Deferred: AI Act art. 50 / C2PA / IPTC `DigitalSourceType` marking (legal is out of scope for now); the AI origin is
  recorded in the file's metadata (`extra.ai`: provider, model, prompt).
- Live test spend (budget 250): 224 kie credits (two 480p 4 s drafts — the first one's download failed in the test
  harness, not the app); 5 failed / refused tasks cost 0.

#### Tests
- Full suite in the prod-like image on a throwaway Postgres: 618 passed, 9 skipped (main: 572 / 9) — 46 new tests in
  `test_credits.py`, `test_mcp_credits.py`, `test_kie_provider.py`, `test_ai_models.py`, `test_ai_enhance.py`,
  `test_ai_clip.py`; kie is always mocked (no network), the timeline tests render with the real engine.

#### Credits + kie.ai — fixes after the independent review

- **One cancel rule** (DELETE /api/ai/task, job cancel, MCP jobs cancel): before a part reaches kie, cancel releases the
  whole hold and nothing starts; once kie has it (kie has no cancel API), the hold stays, the user pays what kie really
  charged (0 if it fails), the result is kept in their files and nothing is put on the timeline. A part that fails to
  start and the 6 h wait timeout no longer release while other parts run; the provider cost is booked when they end.
  Closes the "start + cancel = free kie money" hole.
- Shared files: an op started on someone else's file stores the owner's workspace and reopens it
  (`sharing.workspace`) when the webhook or the resume loop finishes it — the result lands next to the source, the
  starter pays.
- Job cancel during prepare/upload releases the hold and never leaves the op "watched" forever.
- The real video length is paid: max(format, stream, packets ÷ fps) — a header patched to "1 s" over 20 s of picture
  is billed 20 s; the Topaz upload and Seedance reference videos are cut with `-t` to the paid seconds.
- Capture is never above the hold (no hold → no charge); a deleted pricing rule is a tombstone (`deleted_at`) and
  isn't re-seeded at boot; a paid AI op with no price (deleted / zero rule) isn't started for non-admins.
  `KIE_CREDIT_MULTIPLIER` only applies to rules seeded for the first time — change existing prices in the admin.
- Users no longer see our cost basis: the credit history leaves out provider cost, kie task id and meta; estimates
  drop `provider_cost` for non-admins.
- The resume loop always runs when the database is on (also with `AI_VIDEO` off).
- Post-processing runs once: its result is stored before settling and reused after a crash; raw kie downloads are
  removed only after settling.
- The `token=` of `/webhooks/kie` is masked in the uvicorn access log.
- Small: a spend on an account without a row is `InsufficientCredits` again; the free monthly top-up is one
  transaction; `/api/ai/estimate` is behind the AI video switch; a finalize estimate needs your own draft.
- Tests: `tests/test_ai_review_fixes.py`, `tests/test_credits_review_fixes.py` (19 tests, all failing on bf29859).

### Shareable workspace — phase 4, 3.1 Save to Photos, location stripping

#### Phase 4 — shareable workspace (lane "share")

- **Sharing (owner / editor / viewer), behind `SHARING_ENABLED=1`** (off by default — off, everything behaves as before:
  workspaces stay physically isolated). Share a file or a video project by user or e-mail; an e-mail without an account
  becomes a pending invite that resolves on sign-up / sign-in (no e-mail is sent — the app has no mail path). Role links
  (`/share/<token>`) work only for signed-in users; signed download links stay read-only.
  A share covers the file's *family* in the owner's workspace: its versions, renders, the project's media assets (from
  every version's `assets` map) and — read-only — the footage it was made from.
- **One ACL gate for every request**: the auth middleware reads every file id in the URL, query, JSON body and MCP
  tool arguments, checks the actor's role and runs the request in the owner's workspace; the acting user stays the
  operation's owner (credits, quotas, jobs, audit). Web: 403 `{"detail", "not_shared": true}`; MCP: a tool error
  (`isError`), no job is started. New Postgres tables: `shares`, `share_links`, `share_audit`.
- **Shared with me**: `/api/files` → `shared` (owner name, role), MCP `files(action="list")` → `shared_with_me`;
  `files(action="share" | "share_link")`. Edits by invitees are new versions in the owner's workspace with
  `edited_by`; the audit log (`share_audit`) records who shared / changed what and when.
- **Retention**: shared projects and their whole family (assets included) are exempt from the 30-day cleanup while
  the share (or a pending invite) is active.
- **Versions with optimistic locking**: every edit stays a new version; if someone else saved a newer version of the
  same base meanwhile (or of the `if_version` the client started from — any MCP tool and the editor save accept it),
  the new one becomes a fork labelled "<Name>'s version", the result carries `conflict {fork, other, message}`, and
  the user picks the current one (`POST /api/share/choose`, MCP `files(action="choose")`); nothing is lost. Same
  person branching an older version is not a conflict. Advisory edit lock with a 30 s heartbeat (stale after 90 s,
  `edit_locks` table) only tells who is editing; it never blocks.
- **Web UI** (only when sharing is on): file view "More → Share…" dialog (invite by e-mail with Can edit / Can view,
  change role, remove, pending invites, link for signed-in people, "keep location" opt-out, recent activity);
  Files → "Shared with me" (owner's name on each card, "By <name>" on versions others made in your workspace); the file
  header tag "<Owner>'s · you can edit"; a "Two versions" bar that lets the user choose the current version after a
  parallel save; a "<Name> is editing this right now" bar from the edit-lock heartbeat.
- **Privacy — GPS / location stripped on share and on export** (`app/storage/privacy.py`, `STRIP_LOCATION=1` default):
  video via `ffmpeg -c copy` with the location keys blanked (`location`, `location-eng`, `©xyz`,
  `com.apple.quicktime.location.ISO6709`; data tracks dropped) — rotation (display matrix), creation time and the other
  metadata are kept, the mtime too; photos via ExifTool (EXIF GPS IFD, XMP/IPTC location fields), Pillow fallback for
  JPEG/PNG without touching pixels. On share the shared family's media is cleaned in place (opt-out `keep_location`);
  on export every outgoing copy is cleaned and cached (`/files/…` downloads incl. Range, image `/preview`, cloud export,
  Save to Photos). If location is present but can't be removed, the file isn't sent (422).
  **Measured: the spec's "pipeline outputs are already clean" is false for the ffmpeg tools** — resize-video,
  trim-video, compress-video, rotate-video and merge-files keep `location`/`location-eng` from an iPhone-like input
  (ffmpeg 4.4 copies global metadata by default; `ios_safe` uses `-map_metadata 0`). The export guard covers them.

#### 3.1 — Save to Photos (iOS)

- File view **"Save to Photos"** button for videos and pictures, shown where the browser can share files (phones): on
  tap it fetches the file with a progress readout, wraps it as a `File` and calls `navigator.share({files})` → the
  share sheet's "Save Video" / "Save Image" puts it in Photos. If the tap's user activation ran out during the
  download (Safari), the button turns into "Ready — tap to save to Photos"; if this file can't be shared, it downloads.
- `GET /api/photos/<file_id>` → `{url, name, type, size, converted}`: MP4/MOV with H.264/HEVC + AAC and
  JPEG/PNG/HEIC/HEIF/GIF go as they are; WebM/MKV/other codecs are transcoded to MP4 (H.264 CRF 20 + AAC 192k,
  faststart) and WebP/TIFF/BMP/PSD to JPEG (PNG with alpha) first, as a job; a video project uses its last render.
  `GET /api/photos/<file_id>/file` serves it with the right Content-Type and file name, location-free. Guests too.
- Native app variant (Capacitor/Expo, PHPhotoLibrary "add only") is left for later.

#### Sharing — ACL hardening after the independent review

- **Fail closed per file**: inside a shared request the owner's workspace resolves only for the shared family (plus
  files the request creates and new versions whose source chain leads into the family); any other owner file is
  403 even if its id slipped past the request scan. Signed links are never minted for such a file.
- **Request scan everywhere**: path, query keys/values, JSON (keys, values, ids inside URLs / comma lists /
  JSON-in-a-string, `\u`-escaped), form-urlencoded (decoded), multipart and text bodies, MCP arguments — up to 64 MB.
  In a shared request a body that can't be checked is refused (413 too large, 415 unknown type); raw-file bodies
  (editor save, video asset) carry their ids in the query. Uploads never enter a shared context.
- **Workspace-wide code stays in the actor's workspace**: `tenancy.user_root()` without an argument is always the
  signed-in user's (listing, temp, delete); the owner's tree only explicitly — `workspace_root()` for new-file naming,
  `visible_root()` (family only) for the version tree and the digitize sibling lookup; pdfcraft `--root` is the
  narrowest root covering the script's (ACL-resolved) paths, owner-side paths must be family members.
- Review inbox / resolve are the actor's own; lineage, live-data bindings, the share activity log (for non-owners)
  and conflict info are filtered to the shared family.
- **Retention on a DB error**: every successful round saves the shared-file list in the workspace root
  (`.shared-keep.json`); if Postgres doesn't answer, the last list (≤ 7 days old) keeps the shared files and the rest
  of the workspace is cleaned as usual; with no list that round skips the workspace — logged as an error.
