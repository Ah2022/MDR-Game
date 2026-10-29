"""A small, persistent Macrodata Refinement fan game. Run: python app.py."""

import json
import os
import random
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

ROWS, COLS = 12, 20
TEMPERS = ("WO", "FC", "DR", "MA")
COOKIE = "mdr_operator"


def new_game():
    rng = random.SystemRandom()
    cells = [{"id": i, "value": rng.randrange(10)} for i in range(ROWS * COLS)]
    assignments = [(route, temper) for route in range(1, 6) for temper in TEMPERS]
    rng.shuffle(assignments)
    groups = []
    for slot, (route, temper) in enumerate(assignments):
        row = (slot // 5) * 3 + rng.randrange(2)
        col = (slot % 5) * 4 + rng.randrange(3)
        members = [row * COLS + col, row * COLS + col + 1,
                   (row + 1) * COLS + col, (row + 1) * COLS + col + 1]
        groups.append({"id": slot, "members": members, "route": route, "temper": temper})
    return {"id": secrets.token_hex(4).upper(), "name": rng.choice(
        ["Siena", "Tumwater", "Allentown", "Dranesville", "Labrador"]),
        "cells": cells, "groups": groups, "refined": [], "mistakes": 0,
        "started": int(time.time()), "finished": None, "revision": 0}


def public_game(state):
    refined = set(state["refined"])
    cells = [dict(cell) for cell in state["cells"]]
    bins = [{"id": route, "count": 0, "tempers": {t: False for t in TEMPERS}}
            for route in range(1, 6)]
    for group in state["groups"]:
        done = group["id"] in refined
        if done:
            bins[group["route"] - 1]["count"] += 1
            bins[group["route"] - 1]["tempers"][group["temper"]] = True
        for index in group["members"]:
            cells[index]["refined"] = done
            if not done:
                cells[index]["signal"] = {"cluster": group["id"],
                                          "route": group["route"], "temper": group["temper"]}
    return {key: state[key] for key in ("id", "name", "revision", "mistakes", "started", "finished")} | {
        "rows": ROWS, "cols": COLS, "cells": cells, "bins": bins,
        "progress": len(refined) * 5, "complete": len(refined) == 20}


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(DATABASE=os.environ.get("MDR_DATABASE", str(Path(app.instance_path) / "mdr.sqlite3")),
                      COOKIE_SECURE=os.environ.get("MDR_COOKIE_SECURE") == "1",
                      MAX_CONTENT_LENGTH=4096)
    if test_config:
        app.config.update(test_config)
    app.json.sort_keys = False
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(app.config["DATABASE"])) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS operators "
                   "(id TEXT PRIMARY KEY, csrf TEXT NOT NULL, state TEXT NOT NULL, updated INTEGER NOT NULL)")
        db.commit()

    def database():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_database(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/game")
    def get_game():
        db = database()
        row = db.execute("SELECT * FROM operators WHERE id = ?", (request.cookies.get(COOKIE, ""),)).fetchone()
        fresh = row is None
        if fresh:
            operator, csrf, state = secrets.token_hex(32), secrets.token_hex(32), new_game()
            db.execute("INSERT INTO operators VALUES (?, ?, ?, ?)",
                       (operator, csrf, json.dumps(state), int(time.time())))
            db.commit()
        else:
            operator, csrf, state = row["id"], row["csrf"], json.loads(row["state"])
        response = jsonify(game=public_game(state), csrf=csrf)
        # Refresh the browser's 30-day retention on every load.
        response.set_cookie(COOKIE, operator, max_age=30 * 86400, httponly=True,
                            secure=app.config["COOKIE_SECURE"], samesite="Lax")
        return response

    def mutate(reset=False):
        if not request.is_json:
            return jsonify(error="Send a JSON request."), 415
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="This request must come from your terminal."), 403
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object."), 400
        db = database()
        # Serialize read/validate/write, including requests from other tabs/workers.
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM operators WHERE id = ?",
                             (request.cookies.get(COOKIE, ""),)).fetchone()
            if row is None:
                return jsonify(error="Session unavailable. Reload your terminal."), 401
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), row["csrf"].encode()):
                return jsonify(error="Session verification failed. Reload your terminal."), 403
            state = json.loads(row["state"])
            if data.get("game_id") != state["id"] or type(data.get("revision")) is not int or data["revision"] != state["revision"]:
                return jsonify(error="Another terminal updated this file. Your view has been refreshed.",
                               game=public_game(state)), 409
            if reset:
                state = new_game()
                message = "New file assigned. The work is mysterious and important."
                accepted = True
            else:
                ids, route = data.get("cells"), data.get("bin")
                if (not isinstance(ids, list) or len(ids) != 4
                        or any(type(i) is not int or not 0 <= i < ROWS * COLS for i in ids)
                        or len(set(ids)) != 4 or type(route) is not int or not 1 <= route <= 5):
                    return jsonify(error="Select exactly four different numbers and a bin from 01 to 05."), 400
                if state["finished"]:
                    return jsonify(error="This file is complete. Request a new assignment.", game=public_game(state)), 409
                group = next((item for item in state["groups"] if set(item["members"]) == set(ids)), None)
                accepted = bool(group and group["route"] == route and group["id"] not in state["refined"])
                if accepted:
                    state["refined"].append(group["id"])
                    message = f"Bin {route:02d} accepted. {group['temper']} balanced. Good work, refiner."
                    if len(state["refined"]) == 20:
                        state["finished"] = int(time.time())
                        message = "File complete. Your contribution has been acknowledged."
                else:
                    state["mistakes"] += 1
                    message = "These numbers do not belong together in that bin. Trust the signal."
                state["revision"] += 1
            db.execute("UPDATE operators SET state = ?, updated = ? WHERE id = ?",
                       (json.dumps(state), int(time.time()), row["id"]))
            db.commit()
            return jsonify(game=public_game(state), accepted=accepted, message=message)
        finally:
            if db.in_transaction:
                db.rollback()

    @app.post("/api/refine")
    def refine():
        return mutate()

    @app.post("/api/reset")
    def reset():
        return mutate(reset=True)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
