"""Shared helpers for the ANO reference scripts.

Credentials are NEVER written to disk: they are read from environment variables
(the same username/password the user types into PuTTY):
    export ANO_USER=admin ANO_PASS='...'      [optional] ANO_SECRET=enable-secret
    export ANO_SSH_KEY=~/.ssh/ano.pem         (PuTTY key converted: puttygen key.ppk -O private-openssh -o ano.pem)
More logins for other device groups: ANO_USER_2/ANO_PASS_2 ... ANO_USER_9/ANO_PASS_9.
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)

# All vendor-specific tables moved to platforms.py (single place to add a vendor).


def _profile(n: int) -> dict | None:
    sfx = "" if n == 1 else f"_{n}"
    user, pw, key = (os.environ.get(f"ANO_USER{sfx}"), os.environ.get(f"ANO_PASS{sfx}"),
                     os.environ.get(f"ANO_SSH_KEY{sfx}"))
    if not user or not (pw or key):
        return None
    c = {"username": user, "password": pw or ""}
    if key:
        c["key_file"] = os.path.expanduser(key)
    if os.environ.get(f"ANO_SECRET{sfx}"):
        c["secret"] = os.environ[f"ANO_SECRET{sfx}"]
    return c


def creds_list() -> dict[int, dict]:
    """Real networks often use different logins per device group. Profile 1 = ANO_USER/ANO_PASS,
    profile 2 = ANO_USER_2/ANO_PASS_2, ... up to 9. Discovery records which profile worked per device."""
    out = {n: p for n in range(1, 10) if (p := _profile(n))}
    if not out:
        sys.exit("Set ANO_USER and ANO_PASS (or ANO_SSH_KEY). ANO does not assume credentials.")
    return out


def creds(profile: int = 1) -> dict:
    lst = creds_list()
    if profile not in lst:
        sys.exit(f"Credential profile {profile} is not set (ANO_USER_{profile}/ANO_PASS_{profile}).")
    return lst[profile]


def load_yaml(p) -> dict:
    return yaml.safe_load(Path(p).read_text())


def dump_yaml(obj, p) -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(yaml.safe_dump(obj, sort_keys=False))


def dump_json(obj, p) -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, indent=2, default=str))


ANSWERS = Path(os.environ.get("ANO_ANSWERS", ROOT / "inputs" / "answers.yaml"))


def answer(qid: str):
    """The user's recorded answer to question <qid>, or None. Answers live in inputs/answers.yaml:
         link:r1:eth1|r2:eth1: direct        (a cable ANO could only infer)
    so a re-run continues instead of asking the same question again."""
    if ANSWERS.exists():
        v = (yaml.safe_load(ANSWERS.read_text()) or {}).get(qid)
        return {True: "yes", False: "no"}.get(v, v) if isinstance(v, bool) else v   # YAML reads yes/no as booleans
    return None


def ask(questions: list) -> None:
    """No-assumption rule: unresolved facts are printed as questions and the step stops.
    A question is a string, or (id, text) when it can be answered in inputs/answers.yaml."""
    if questions:
        print("\nANO needs answers before it can continue:")
        for i, q in enumerate(questions, 1):
            if isinstance(q, tuple):
                print(f"  Q{i}. {q[1]}\n       answer in {ANSWERS.name} as   {q[0]!r}: <answer>")
            else:
                print(f"  Q{i}. {q}")
        dump_json([q if isinstance(q, str) else {"id": q[0], "question": q[1]} for q in questions],
                  OUT / "open_questions.json")
        sys.exit(2)
