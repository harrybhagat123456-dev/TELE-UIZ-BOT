# 🤖 Quiz Practice Bot — Complete Documentation

Ek hi Telegram bot me:
  1. **Scanner** — source channel se question image, poll, aur ~100 msg baad wali explanation code (`SC12`) se links karke DB me daalta hai.
  2. **Generator** — `/gen SC1-90` command admin chalata hai → subject (`PHYSICS`) → topic (`Kinematics`) poochta hai → practice channel me 90 quiz polls daalta hai.
  3. **Tracker** — continuous voter listener + auto-reveal background worker (10 min open, auto-close + solution reveal).
  4. **SRS Spaced Repetition (SM-2)** — wrong questions automatically user ke review queue me chale jate hain.
  5. **Flask Dashboard (Koyeb)** — `Subject → Topic → Flashcards (Flip for explanation)`.

---

## 🚀 Koyeb Deployment (Step-by-Step)

### Step 1: GitHub Repo Banayo
1. GitHub pe ek **new repository** standard `quiz-practice-bot` ke naam se banayo.
2. Local folder `C:\Users\harry\quiz-practice-bot` ko git push karo:
```bash
cd C:\Users\harry\quiz-practice-bot
git init
git add .
git commit -m "Initial commit: Quiz practice bot with Flask flashcards dashboard"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/quiz-practice-bot.git
git push -u origin main
```

### Step 2: Koyeb pe Service Create karo
1. [koyeb.com](https://www.koyeb.com) pe Login/Signup karo.
2. **Create App / Create Service** button pe click karo.
3. Source type: **GitHub** choose karo aur apna repo `quiz-practice-bot` select karo.
4. Builder: **Dockerfile** select karo.
5. Port: **8080** (Protocol: HTTP, Path: `/health`).

### Step 3: Environment Variables (Koyeb Dashboard me daalo)

| Env Variable | Meaning / Example |
|--------------|-------------------|
| `API_ID` | Telegram API ID (my.telegram.org) |
| `API_HASH` | Telegram API Hash |
| `BOT_TOKEN` | BotFather se mila bot token |
| `MONGO_DB` | MongoDB Atlas Connection String (`mongodb+srv://...`) |
| `DB_NAME` | `quiz_practice_bot` |
| `PRACTICE_CHAT` | Practice channel jahan polls bhejni hain (e.g. `@my_quiz_practice_channel` ya `-100...`) |
| `OWNER_ID` | Tera Telegram User ID (space separated agar multiple admins hain) |
| `DASHBOARD_SECRET` | Dashboard login ka password (e.g. `mysecret123`) |
| `DASH_ALLOWED_TG_IDS` | Tera User ID (dashboard login access ke liye) |
| `PORT` | `8080` |

6. **Deploy** pe click karo. 1-2 min me service Live ho jayegi!

---

## 📱 Bot Commands & Workflows

### Admin Workflow

1. **Source channel scan karo:**
   `/scan @sourcechannel`
   *(Yeh channel history walk karke saare SC1, SC2... questions, options, correct index, solution scan karke MongoDB me daal dega)*

2. **Practice polls generate karo:**
   `/gen SC1-90`
   - Bot poochega: **Subject** (PHYSICS / CHEMISTRY / BIOLOGY / MATHS / OTHER)
   - Phir poochega: **Topic** (Kinematics / Thermodynamics / custom)
   - Phir practice channel me 90 quiz polls sequence me bhej dega.

---

### User Workflow & Telegram Dashboard

- `/stats` — Accuracy %, correct/attempted count, streak, subject-wise breakdown.
- `/wrong` — Teri wrong attempt kiye gaye questions with options, correct answer & explanation.
- `/review` — Spaced repetition (SM-2) algorithm ke according jo flashcards aaj revise hone hain.
- `/bookmarks` or `/bm SC12` — Questions bookmark / unbookmark karne ke liye.
- `/leaderboard` — Subhi users ka top scores.

---

## 🌐 Web Dashboard (Koyeb Public URL)

1. Browser me apna Koyeb App URL kholo (e.g. `https://quiz-practice-bot-xyz.koyeb.app/login`).
2. Enter **Telegram User ID** + **DASHBOARD_SECRET**.
3. **Subjects View**: HAR subject ke under total questions & wrong count visible hoga.
4. **Topics View**: Subject click karne par uske saare topics show honge.
5. **Flashcards View**: Topic click karne par 3D Interactive Flashcards khulenge:
   - **Front**: Question Image + Options (A/B/C/D) + Tera wrong answer + Correct answer highlight.
   - **Flip (Spacebar / Tap)**: Explanation text & solution visible.
   - Spaced Repetition (SM-2) sorting: Most overdue & difficult questions pehle aate hain.
