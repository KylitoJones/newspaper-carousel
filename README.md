# Newsstand

A private website that shows **today's newspaper front pages** from three
sources — [Freedom Forum](https://frontpages.freedomforum.org/),
[FrontPages.com](https://www.frontpages.com/), and
[Kiosko.net](https://en.kiosko.net/) — merged into one grid with duplicates
removed. It refreshes itself every morning around 8 a.m. Mountain. You browse,
tap up to **four** pages you like, tweak the post text, and one command
publishes them to Bluesky as a single post — with alt text written
automatically for each image.

Papers are grouped by **region, in priority order**: United States, Canada,
Mexico, Western Europe, Central America & Caribbean, South America, then
everywhere else. So the papers you reach for most are always at the top, but
the whole world is still there if you scroll.

## What's in here

| File | What it does |
|------|--------------|
| `scrape.py` | Pulls all three sources, removes duplicates, ranks by region, writes `site/manifest.json`. Edit `REGION_ORDER` at the top to change the priority. |
| `site/index.html` | The browse-and-select page (this is what GitHub Pages serves). |
| `post.py` | Reads your picks, writes alt text with Claude, posts to Bluesky. |
| `.github/workflows/build.yml` | Runs the scrape every morning + on demand, and deploys the site. |
| `aliases.json` | Manual "these two names are the same paper" list (see Tuning). |
| `.env.example` | Template for your secrets. Copy to `.env`. |

---

## Part 1 — Put it on GitHub (one time)

1. Create a new repository on GitHub and push these files to it.
2. In the repo, go to **Settings → Pages** and set **Source: GitHub Actions**.
3. Go to the **Actions** tab, pick **Build front pages**, and click
   **Run workflow**. This does the first scrape and publishes the site.
4. Your site appears at `https://<your-username>.github.io/<repo>/`.

From then on it rebuilds automatically at 14:00 and 15:00 UTC — that's 8 a.m.
Mountain across both daylight and standard time. (GitHub occasionally delays
scheduled runs by a few minutes, so it's "about" 8 a.m.)

### Force a scrape any time

Two ways, and they do the same thing:

- **On GitHub:** Actions tab → **Build front pages** → **Run workflow**.
- **On your computer:** `python scrape.py` (then commit/push, or just use it to
  preview locally).

Use this to test now, or later in the day to pick up a paper that posted late.

---

## Part 2 — Set up posting (one time, on your computer)

You only need this for the "post to Bluesky" step. Everything stays local.

1. **Install Python 3.10+**, then in this folder:
   ```
   pip install -r requirements.txt
   ```
2. **Make a Bluesky app password** (not your real password): Bluesky →
   Settings → Privacy and Security → **App Passwords → Add App Password**.
   Copy it.
3. **Get an Anthropic API key** from the Anthropic Console (this writes the alt
   text). A few images a day costs pennies.
4. **Create your `.env`:** copy `.env.example` to `.env` and fill in the three
   values. Your handle is already set to `kanasjones.bsky.social`.

---

## Daily routine

1. Open your site sometime after 8 a.m.
2. Tap between **one and four** front pages. A tray slides up with your picks.
3. Edit the post text if you like (it's pre-filled with the date; 300-char
   limit is shown live).
4. Click **Prepare post**. This saves `bluesky-post.json` to your Downloads.
5. Run the poster:
   - **Mac:** double-click `post.command` (first time: right-click → Open).
   - **Any system:** `python post.py`
6. It fetches your images, writes alt text, resizes them, and posts. It prints
   the link to your new post.

Want to see the alt text before anything goes live? Run `python post.py --dry-run`.

---

## Tuning

- **Reorder the regions / redefine "Western Europe."** The priority order lives
  in one editable list at the top of `scrape.py` called `REGION_ORDER`. Each
  entry is a group name and a set of country codes (Kiosko's codes — note "uk",
  not "gb"). Move groups around, add codes, or split them however you like;
  anything not listed falls into "Everywhere else" at the end.
- **Merge duplicates that are named differently.** The three sources sometimes
  name the same paper differently (e.g. "The Minnesota Star Tribune" vs
  "Star Tribune", or Kiosko's "Daily News - New York"). Add a line to
  `aliases.json` mapping one to the other and they'll collapse into one card.
  With three sources you'll add a few of these over time; a starter set is seeded.
- **Which source wins a tie.** When two sources have the same paper for the same
  day, Freedom Forum's copy is kept by default (usually higher-res). Change with
  `python scrape.py --prefer Kiosko` (or `FrontPages.com`), or edit `build.yml`.
- **Filter while browsing.** The page has a name search plus region and source
  filters, and jumps straight to any region.
- **Country coverage.** Kiosko provides the country (and therefore region) for
  almost everything. Freedom Forum is treated as U.S.; FrontPages.com papers
  outside its US/UK sections may land in "Everywhere else" unless the same paper
  also comes from Kiosko (in which case the country carries over automatically).

---

## If something looks off

- **A source came back with far fewer papers than expected.** Run
  `python scrape.py --verbose` to see exactly what each source returned. These
  are live third-party sites; if one changes its page layout, the matching
  section in `scrape.py` (`scrape_freedomforum` / `scrape_frontpages`) is where
  a small selector tweak goes. The scraper refuses to overwrite a good manifest
  with an empty one, so a bad run won't wipe your site.
- **A cover shows "image unavailable."** That paper hadn't posted its front
  page yet when the scrape ran. Force a scrape later and it'll fill in.
- **Bluesky rejected the post.** Check the text is ≤ 300 characters and you
  picked ≤ 4 images. Images are auto-shrunk under Bluesky's ~1 MB limit, and
  the full page is visible when someone taps the image (feeds show a cropped
  preview).

---

## A note on the images

Newspaper front pages are copyrighted, and both sites display them under their
own arrangements. This tool is set up for you to browse privately and hand-pick
a few to share with attribution — the everyday "today's front pages" use. It
re-shares nothing on its own and the browse site isn't public unless you make
it so. Where your own line is on what to post is your call.
