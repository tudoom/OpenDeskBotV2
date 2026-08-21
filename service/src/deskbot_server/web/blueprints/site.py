from __future__ import annotations

from flask import Blueprint, redirect, url_for

bp = Blueprint("site", __name__)


@bp.get("/")
def index():
    # Opening this single-PC appliance enters its local control console.
    return redirect(url_for("app2c.home"))
