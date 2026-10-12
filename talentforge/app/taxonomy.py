"""Skill taxonomy and role catalogue (Phase 1).

Deliberately small and hand-curated so skill-gap results are explainable and measurable.
"""
from __future__ import annotations

# canonical skill -> (category, [aliases])
SKILLS: dict[str, tuple[str, list[str]]] = {
    # languages
    "python": ("language", ["python", "python3"]),
    "java": ("language", ["java", "core java"]),
    "javascript": ("language", ["javascript", "js", "es6", "ecmascript"]),
    "typescript": ("language", ["typescript"]),
    "c++": ("language", ["c++", "cpp"]),
    "c#": ("language", ["c#", "csharp"]),
    "go": ("language", ["golang"]),
    "sql": ("database", ["sql", "t-sql", "pl/sql"]),
    "html": ("web", ["html", "html5"]),
    "css": ("web", ["css", "css3", "sass", "scss", "tailwind", "tailwind css", "bootstrap"]),
    # frameworks
    "react": ("framework", ["react", "react.js", "reactjs", "next.js", "nextjs"]),
    "angular": ("framework", ["angular", "angularjs"]),
    "vue": ("framework", ["vue", "vue.js", "vuejs"]),
    "node.js": ("framework", ["node.js", "nodejs", "node js", "express", "express.js"]),
    "django": ("framework", ["django"]),
    "flask": ("framework", ["flask"]),
    "fastapi": ("framework", ["fastapi", "fast api"]),
    "spring boot": ("framework", ["spring boot", "springboot", "spring"]),
    "rest api": ("backend", ["rest api", "rest apis", "restful", "rest", "api development", "web services"]),
    "graphql": ("backend", ["graphql"]),
    # data stores
    "postgresql": ("database", ["postgresql", "postgres"]),
    "mysql": ("database", ["mysql", "mariadb"]),
    "sqlite": ("database", ["sqlite"]),
    "mongodb": ("database", ["mongodb", "mongo"]),
    "redis": ("database", ["redis"]),
    # devops / cloud
    "git": ("tooling", ["git", "github", "gitlab", "version control"]),
    "docker": ("devops", ["docker", "containerization", "containers"]),
    "kubernetes": ("devops", ["kubernetes", "k8s"]),
    "aws": ("cloud", ["aws", "amazon web services", "ec2", "s3 bucket"]),
    "azure": ("cloud", ["azure", "microsoft azure"]),
    "gcp": ("cloud", ["gcp", "google cloud"]),
    "linux": ("devops", ["linux", "ubuntu", "bash", "shell scripting", "unix"]),
    "ci/cd": ("devops", ["ci/cd", "cicd", "github actions", "jenkins", "gitlab ci", "continuous integration"]),
    "terraform": ("devops", ["terraform", "infrastructure as code"]),
    "monitoring": ("devops", ["monitoring", "prometheus", "grafana", "observability"]),
    # data / ML
    "machine learning": ("ml", ["machine learning", "ml", "supervised learning"]),
    "deep learning": ("ml", ["deep learning", "neural network", "neural networks", "cnn", "rnn"]),
    "nlp": ("ml", ["nlp", "natural language processing", "text mining"]),
    "pandas": ("data", ["pandas"]),
    "numpy": ("data", ["numpy"]),
    "scikit-learn": ("ml", ["scikit-learn", "sklearn", "scikit learn"]),
    "tensorflow": ("ml", ["tensorflow"]),
    "pytorch": ("ml", ["pytorch", "torch"]),
    "keras": ("ml", ["keras"]),
    "statistics": ("data", ["statistics", "statistical analysis", "hypothesis testing", "probability"]),
    "data visualization": ("data", ["data visualization", "data visualisation", "matplotlib", "seaborn", "plotly"]),
    "power bi": ("data", ["power bi", "powerbi"]),
    "tableau": ("data", ["tableau"]),
    "excel": ("data", ["excel", "ms excel", "microsoft excel", "advanced excel"]),
    # GenAI
    "llm": ("genai", ["llm", "llms", "large language model", "large language models", "gpt", "openai", "claude", "gemini", "generative ai"]),
    "prompt engineering": ("genai", ["prompt engineering", "prompt design", "prompting"]),
    "rag": ("genai", ["rag", "retrieval augmented generation", "retrieval-augmented generation"]),
    "vector database": ("genai", ["vector database", "vector db", "faiss", "pinecone", "chroma", "chromadb", "pgvector", "weaviate"]),
    "langchain": ("genai", ["langchain", "langgraph", "llamaindex"]),
    # QA
    "testing": ("qa", ["testing", "unit testing", "test automation", "manual testing", "test cases", "qa"]),
    "selenium": ("qa", ["selenium", "webdriver"]),
    "pytest": ("qa", ["pytest", "unittest", "junit"]),
    "cypress": ("qa", ["cypress", "playwright"]),
    # soft
    "agile": ("process", ["agile", "scrum", "kanban"]),
    "communication": ("soft", ["communication", "communication skills", "presentation"]),
    "teamwork": ("soft", ["teamwork", "collaboration", "team player"]),
    "problem solving": ("soft", ["problem solving", "problem-solving", "analytical thinking"]),
}

# Skills that imply a more general skill (holding the key counts as holding the value)
IMPLIES: dict[str, list[str]] = {
    "postgresql": ["sql"], "mysql": ["sql"], "sqlite": ["sql"],
    "react": ["javascript"], "angular": ["javascript"], "vue": ["javascript"],
    "node.js": ["javascript"], "typescript": ["javascript"],
    "django": ["python", "rest api"], "flask": ["python", "rest api"], "fastapi": ["python", "rest api"],
    "pandas": ["python"], "numpy": ["python"], "scikit-learn": ["python", "machine learning"],
    "tensorflow": ["machine learning", "deep learning"], "pytorch": ["machine learning", "deep learning"],
    "keras": ["machine learning", "deep learning"],
    "deep learning": ["machine learning"],
    "langchain": ["llm"], "rag": ["llm"], "prompt engineering": ["llm"],
    "selenium": ["testing"], "pytest": ["testing"], "cypress": ["testing"],
    "kubernetes": ["docker"],
    "spring boot": ["java", "rest api"],
    "power bi": ["data visualization"], "tableau": ["data visualization"],
}

# Interchangeable groups: holding one earns partial credit (0.5) for another
RELATED_GROUPS: list[set[str]] = [
    {"flask", "django", "fastapi", "node.js", "spring boot"},
    {"aws", "azure", "gcp"},
    {"tensorflow", "pytorch", "keras"},
    {"power bi", "tableau", "data visualization"},
    {"react", "angular", "vue"},
    {"selenium", "cypress", "pytest", "testing"},
    {"postgresql", "mysql", "mongodb", "sqlite", "sql"},
    {"python", "java", "c++", "c#", "go"},
    {"vector database", "redis"},
    {"langchain", "rag", "llm", "prompt engineering"},
    {"docker", "kubernetes"},
]

# role id -> definition. required: skill -> importance weight (1-3)
ROLES: dict[str, dict] = {
    "backend": {
        "title": "Backend Developer",
        "description": "Builds server-side services, APIs and data layers.",
        "target_years": 2,
        "required": {"python": 3, "sql": 3, "rest api": 3, "git": 2, "fastapi": 2, "docker": 2, "linux": 1},
        "preferred": ["postgresql", "redis", "aws", "ci/cd", "testing"],
    },
    "frontend": {
        "title": "Frontend Developer",
        "description": "Builds responsive user interfaces for web applications.",
        "target_years": 2,
        "required": {"javascript": 3, "html": 3, "css": 3, "react": 3, "git": 2, "typescript": 2, "rest api": 2},
        "preferred": ["testing", "ci/cd", "node.js"],
    },
    "fullstack": {
        "title": "Full-Stack Developer",
        "description": "Owns features end to end, from UI to database.",
        "target_years": 2,
        "required": {"javascript": 3, "react": 2, "node.js": 2, "python": 2, "sql": 3, "rest api": 3, "git": 2, "html": 1, "css": 1},
        "preferred": ["docker", "typescript", "aws", "testing"],
    },
    "data_analyst": {
        "title": "Data Analyst",
        "description": "Turns data into insight with SQL, statistics and dashboards.",
        "target_years": 1,
        "required": {"sql": 3, "excel": 2, "python": 2, "pandas": 2, "data visualization": 3, "statistics": 3, "power bi": 2, "communication": 1},
        "preferred": ["tableau", "numpy", "machine learning"],
    },
    "data_scientist": {
        "title": "Data Scientist / ML Engineer",
        "description": "Builds and evaluates machine-learning models.",
        "target_years": 2,
        "required": {"python": 3, "machine learning": 3, "statistics": 3, "pandas": 2, "numpy": 2, "scikit-learn": 2, "sql": 2, "deep learning": 1},
        "preferred": ["pytorch", "tensorflow", "nlp", "docker"],
    },
    "devops": {
        "title": "DevOps / Cloud Engineer",
        "description": "Automates delivery and runs reliable cloud infrastructure.",
        "target_years": 2,
        "required": {"linux": 3, "docker": 3, "kubernetes": 2, "ci/cd": 3, "git": 2, "aws": 3, "python": 1},
        "preferred": ["terraform", "monitoring", "azure"],
    },
    "qa": {
        "title": "QA / Test Engineer",
        "description": "Designs and automates tests to protect release quality.",
        "target_years": 1,
        "required": {"testing": 3, "selenium": 2, "python": 1, "sql": 1, "rest api": 2, "git": 1, "agile": 1, "communication": 1},
        "preferred": ["pytest", "ci/cd", "cypress"],
    },
    "llm_engineer": {
        "title": "AI / LLM Application Engineer",
        "description": "Builds products on LLMs, retrieval and agent workflows.",
        "target_years": 1,
        "required": {"python": 3, "llm": 3, "prompt engineering": 2, "rag": 3, "vector database": 2, "rest api": 2, "nlp": 2, "fastapi": 1, "git": 1},
        "preferred": ["langchain", "docker", "machine learning"],
    },
}

# How to build each skill when it is missing (generic, no external links)
LEARNING_TIPS: dict[str, str] = {
    "category:language": "Complete a structured course, then build a small command-line project.",
    "category:framework": "Build and deploy a small CRUD app using it; document the repo with a README.",
    "category:database": "Design a schema for a mini project and write queries for 10 real questions.",
    "category:devops": "Containerise or automate one of your existing projects and publish the pipeline.",
    "category:cloud": "Deploy a small app to the free tier and note the services you used.",
    "category:ml": "Reproduce a notebook on a public dataset and report metrics honestly.",
    "category:data": "Analyse a public dataset and publish findings with charts.",
    "category:genai": "Build a small retrieval-augmented Q&A over your own documents.",
    "category:qa": "Write an automated test suite for an open-source or personal project.",
    "default": "Build one small project that uses it and list it on your resume with the outcome.",
}


def build_alias_index() -> list[tuple[str, str]]:
    """Return (alias, canonical) pairs, longest alias first so longer phrases win."""
    pairs = [(a.lower(), canon) for canon, (_, aliases) in SKILLS.items() for a in aliases]
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def expand_with_implied(skills: set[str]) -> set[str]:
    out = set(skills)
    changed = True
    while changed:
        changed = False
        for s in list(out):
            for implied in IMPLIES.get(s, []):
                if implied not in out:
                    out.add(implied)
                    changed = True
    return out


def related_to(skill: str) -> set[str]:
    rel: set[str] = set()
    for grp in RELATED_GROUPS:
        if skill in grp:
            rel |= grp
    rel.discard(skill)
    return rel


def learning_tip(skill: str) -> str:
    cat = SKILLS.get(skill, ("", []))[0]
    return LEARNING_TIPS.get(f"category:{cat}", LEARNING_TIPS["default"])
