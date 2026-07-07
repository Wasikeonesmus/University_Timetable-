# 🎓 University Timetable System

> An intelligent, automated academic scheduling platform built with Django and Google OR-Tools.

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://python.org)
[![Django](https://img.shields.io/badge/Django-5.2+-green?logo=django)](https://djangoproject.com)
[![OR-Tools](https://img.shields.io/badge/OR--Tools-9.10+-orange?logo=google)](https://developers.google.com/optimization)
[![Docker](https://img.shields.io/badge/Docker-ready-blue?logo=docker)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

---

## 📋 Overview

The **University Timetable System** is a full-featured, multi-university academic scheduling platform that automates the complex process of creating conflict-free timetables. It uses **Constraint Programming** (Google OR-Tools CP-SAT solver) to optimally assign courses, lecturers, rooms, and student groups to time slots — respecting hard and soft constraints automatically.

---

## ✨ Key Features

| Feature | Description |
|---|---|
| 🤖 **Automated Scheduling** | CP-SAT solver generates conflict-free timetables instantly |
| 🏫 **Multi-University** | Manage multiple universities, campuses, faculties & departments |
| 👥 **Role-Based Access** | Admin, Scheduler, Lecturer, and Student roles |
| 🔴 **Real-time Updates** | Firebase integration for live status notifications |
| 📅 **Google Calendar Sync** | Export sessions directly to lecturers' Google Calendars |
| ⚠️ **Conflict Detection** | Automated detection and reporting of scheduling conflicts |
| 📤 **Multiple Exports** | ICS, CSV, PDF, Word, and Excel export formats |
| 📥 **Bulk Import** | Excel-based bulk import for courses, lecturers, rooms & groups |
| 🐳 **Docker Ready** | Fully containerized deployment with `docker-compose` |
| 🔗 **REST API** | Full API powered by Django REST Framework |

---

## 🏗️ Architecture

```
University_Timetable/
├── accounts/              # User authentication & profiles (Google OAuth)
├── scheduler/             # Core scheduling functionality
│   ├── api/               # REST API endpoints (DRF ViewSets)
│   ├── templates/         # HTML templates (Jinja2 / Django)
│   ├── templatetags/      # Custom template tags
│   └── management/        # Django management commands
├── timetable_project/     # Django project settings & URLs
├── Dockerfile             # Container image definition
├── docker-compose.yml     # Multi-service orchestration
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
└── manage.py              # Django entry point
```

### Organizational Hierarchy

```
University
└── Campus
    ├── Building → Room
    └── Faculty
        └── Department
            └── Program
                ├── Course
                └── StudentGroup
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- pip / virtualenv
- (Optional) Docker & Docker Compose

---

### 🐳 Option A — Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/Wasikeonesmus/University_Timetable-.git
cd University_Timetable-

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your settings

# Start all services
docker-compose up --build
```

The app will be available at **http://localhost:8000**

---

### 🐍 Option B — Local Development

```bash
# 1. Clone the repo
git clone https://github.com/Wasikeonesmus/University_Timetable-.git
cd University_Timetable-

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your SECRET_KEY, Firebase credentials, Google OAuth keys, etc.

# 5. Run database migrations
python manage.py migrate

# 6. Create a superuser
python manage.py createsuperuser

# 7. Start the development server
python manage.py runserver
```

Visit **http://127.0.0.1:8000** in your browser.

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in the required values:

```env
SECRET_KEY=your-django-secret-key
DEBUG=True

# Database (SQLite by default, configure PostgreSQL for production)
DATABASE_URL=sqlite:///db.sqlite3

# Google OAuth (for Calendar integration)
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

# Firebase (for real-time updates)
FIREBASE_CREDENTIALS_PATH=firebase_credentials.json
```

> **Important**: Never commit your `.env` or `client_secret.json` files. They are excluded by `.gitignore`.

---

## 🧠 Scheduling Engine

The scheduling engine uses **Google OR-Tools CP-SAT** (Constraint Programming — Satisfiability) solver:

- Assigns `(Course, Lecturer, Room, StudentGroup, TimeSlot)` tuples
- Enforces **hard constraints**: no double-booking of lecturers/rooms/student groups
- Optimises **soft constraints**: preferred time slots, room capacity, lecturer availability
- Background execution via **Django-Q2** async task queue
- Real-time progress updates via Firebase

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `Django >= 5.2` | Web framework |
| `djangorestframework` | REST API |
| `ortools` | CP-SAT scheduling solver |
| `django-q2` | Async task queue |
| `openpyxl` | Excel import/export |
| `reportlab` | PDF generation |
| `python-docx` | Word document export |
| `google-api-python-client` | Google Calendar API |
| `firebase-admin` | Firebase real-time DB |
| `gunicorn` | Production WSGI server |

---

## 🧪 Running Tests

```bash
pytest
# or with Django settings explicitly
pytest --ds=timetable_project.settings
```

---

## 📤 Data Import

Bulk data can be imported via Excel files. Sample templates are included:

| File | Description |
|---|---|
| `courses_import_1000.xlsx` | 1,000-course sample |
| `lecturers_import_1000.xlsx` | 1,000-lecturer sample |
| `rooms_import_1000.xlsx` | 1,000-room sample |
| `student_groups_import_1000.xlsx` | 1,000-group sample |

---

## 🗺️ Roadmap

- [ ] PostgreSQL production database support
- [ ] Multi-language (i18n) support
- [ ] Mobile-responsive PWA frontend
- [ ] Advanced reporting & analytics dashboard
- [ ] Integration with university ERP systems
- [ ] AI-based timetable preference learning

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

---

## 👤 Author

**Wasike Onesmus**  
GitHub: [@Wasikeonesmus](https://github.com/Wasikeonesmus)

---

> 📚 For full technical documentation, see [DOCUMENTATION.md](DOCUMENTATION.md).
