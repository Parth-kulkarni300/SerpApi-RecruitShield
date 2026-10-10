# RecruitShield AI — Frontend

React 19 + TypeScript + Vite dashboard for the RecruitShield AI autonomous candidate screening platform with live SerpApi Google Knowledge Graph & Maps verification.

## 🚀 Live Demo
[serp-api-recruit-shield.vercel.app](https://serp-api-recruit-shield.vercel.app)

## 🛠️ Tech Stack
- **React 19** + **TypeScript** — Component-based UI
- **Vite 8** — Lightning-fast dev server and bundler
- **Vanilla CSS** — Custom glassmorphism dark-mode design system
- **Lucide React** — Icon library
- **Framer Motion** — Animations

## 📦 Setup

```bash
npm install
npm run dev
```

Runs on `http://localhost:5173`

The frontend connects to the FastAPI backend. Set `VITE_API_URL` in a `.env` file to point to your backend:

```
VITE_API_URL=http://localhost:8000
```

## 🏗️ Key Files
- `src/App.tsx` — Full recruiter dashboard (~5000 lines): candidate pipeline, AI chatbot, agent telemetry console, Excel export
- `src/index.css` — Complete design system with CSS tokens, glassmorphism, animations
- `index.html` — Entry point with Google Fonts

## 📖 Full Documentation
See the root [README.md](../README.md) for full system architecture, backend setup, and API documentation.

