import os
os.environ["DATABASE_URL"] = "sqlite:///./test.db"

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import payment_client, mailer, reminders
from app.database import Base, engine

client = TestClient(app)


def admin_headers():
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(payment_client, "charge",
                        lambda amount, email: {"status": "success", "reference": "PAY-TEST"})
    monkeypatch.setattr(payment_client, "refund",
                        lambda amount, reference: {"status": "refunded", "reference": "REF-TEST"})


def make_event(**kw):
    body = {"name": "Hackathon", "price": 100, "capacity": 2, "venue": "Hall A", "date": "2026-12-01"}
    body.update(kw)
    return client.post("/events", json=body, headers=admin_headers()).json()


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_list_events():
    make_event()
    r = client.get("/events")
    assert r.status_code == 200 and r.json()[0]["seats_left"] == 2


def test_search_events():
    make_event(name="Dance Battle")
    make_event(name="Coding Contest")
    assert len(client.get("/events", params={"q": "dance"}).json()) == 1


def test_register_returns_ticket_and_qr():
    ev = make_event()
    r = student_post("/register", json={"event_id": ev["id"], "student_name": "Asha", "student_email": "a@x.com"})
    assert r.status_code == 201
    code = r.json()["code"]
    qr = client.get(f"/tickets/{code}/qr.png")
    assert qr.status_code == 200 and qr.headers["content-type"] == "image/png"


def test_sold_out():
    ev = make_event(capacity=1)
    body = {"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com"}
    assert student_post("/register", json=body).status_code == 201
    assert student_post("/register", json=body).status_code == 409


def test_payment_failure(monkeypatch):
    monkeypatch.setattr(payment_client, "charge", lambda a, e: {"status": "failed"})
    ev = make_event()
    r = student_post("/register", json={"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com"})
    assert r.status_code == 402


def test_checkin_once_only():
    ev = make_event()
    code = student_post("/register", json={"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com"}).json()["code"]
    assert client.post(f"/tickets/{code}/checkin", headers=admin_headers()).status_code == 200
    assert client.post(f"/tickets/{code}/checkin", headers=admin_headers()).status_code == 409


def test_admin_stats():
    ev = make_event()
    student_post("/register", json={"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com"})
    s = client.get("/admin/stats", headers=admin_headers()).json()
    assert s["total_registrations"] == 1 and s["total_revenue"] == 100


def reg(ev, name="A", email="a@x.com"):
    return student_post("/register", json={"event_id": ev["id"], "student_name": name, "student_email": email}).json()


def test_login_wrong_password():
    r = client.post("/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_admin_routes_need_login():
    assert client.post("/events", json={"name": "X"}).status_code == 401
    assert client.get("/admin/stats").status_code == 401
    assert client.delete("/events/1").status_code == 401
    assert client.get("/admin/stats", headers={"Authorization": "Bearer fake.token"}).status_code == 401


def test_public_pages_need_no_login():
    assert client.get("/events").status_code == 200


def test_delete_event():
    ev = make_event()
    assert client.delete(f"/events/{ev['id']}", headers=admin_headers()).status_code == 204
    assert client.get("/events").json() == []


def test_manual_reminder_emails_only_not_checked_in(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, sub, body: sent.append(to) or "sent")
    ev = make_event(capacity=5)
    reg(ev, "A", "a@x.com")
    b = reg(ev, "B", "b@x.com")
    client.post(f"/tickets/{b['code']}/checkin", headers=admin_headers())
    r = client.post(f"/admin/events/{ev['id']}/remind", headers=admin_headers())
    assert r.json()["sent"] == 1 and sent == ["a@x.com"]


def test_auto_reminder_runs_once(monkeypatch):
    from datetime import date, timedelta
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, sub, body: sent.append(to) or "sent")
    ev = make_event(date=(date.today() + timedelta(days=1)).isoformat())
    reg(ev)
    reminders.run_auto_reminders()
    reminders.run_auto_reminders()  # second run must not email again
    assert sent == ["a@x.com"]


# ---------- team events ----------
def team_body(ev, team="Alpha", members=1):
    return {"event_id": ev["id"], "student_name": "Lead", "student_email": "lead@x.com", "team_name": team,
            "members": [{"name": f"M{i}", "email": f"m{i}@x.com"} for i in range(members)]}


def test_team_event_fields_returned():
    ev = make_event(min_team_size=2, max_team_size=4)
    assert ev["min_team_size"] == 2 and ev["max_team_size"] == 4
    assert make_event(name="Solo")["max_team_size"] == 1


def test_invalid_team_size_rejected():
    r = client.post("/events", json={"name": "X", "min_team_size": 3, "max_team_size": 2}, headers=admin_headers())
    assert r.status_code == 400
    r = client.post("/events", json={"name": "X", "min_team_size": 0, "max_team_size": 2}, headers=admin_headers())
    assert r.status_code == 400


def test_team_registration_uses_one_seat():
    ev = make_event(min_team_size=2, max_team_size=4, capacity=3)
    r = student_post("/register", json=team_body(ev, members=2))
    assert r.status_code == 201
    t = r.json()
    assert t["team_name"] == "Alpha" and t["team_size"] == 3 and len(t["members"]) == 2
    assert client.get("/events").json()[0]["seats_left"] == 2  # one team = one seat


def test_team_size_and_name_rules():
    ev = make_event(min_team_size=2, max_team_size=3)
    assert student_post("/register", json=team_body(ev, members=0)).status_code == 422   # too small
    assert student_post("/register", json=team_body(ev, members=3)).status_code == 422   # too big
    assert student_post("/register", json=team_body(ev, team="  ", members=1)).status_code == 422
    assert student_post("/register", json=team_body(ev, members=1)).status_code == 201


def test_individual_event_rejects_members():
    ev = make_event()
    assert student_post("/register", json=team_body(ev, members=1)).status_code == 422
    assert reg(ev)["team_size"] == 1


def test_stats_show_teams_not_participants():
    team_ev = make_event(name="Hack", min_team_size=2, max_team_size=4, capacity=5)
    solo_ev = make_event(name="Quiz", capacity=5)
    student_post("/register", json=team_body(team_ev, members=2))
    reg(solo_ev)
    s = client.get("/admin/stats", headers=admin_headers()).json()
    assert s["total_teams"] == 1 and s["total_individuals"] == 1 and s["total_people"] == 4
    hack = next(e for e in s["events"] if e["event"] == "Hack")
    assert hack["team_event"] is True and hack["registered"] == 1 and hack["people"] == 3


def test_registrations_list_has_team_details():
    ev = make_event(min_team_size=2, max_team_size=4)
    student_post("/register", json=team_body(ev, team="Zeta", members=2))
    row = client.get(f"/admin/events/{ev['id']}/registrations", headers=admin_headers()).json()[0]
    assert row["team_name"] == "Zeta" and row["team_size"] == 3 and len(row["members"]) == 2


def test_checkins_list_shows_holder_name():
    ev = make_event()
    t = reg(ev, "Asha", "asha@x.com")
    assert client.get("/admin/checkins").status_code == 401
    assert client.get("/admin/checkins", headers=admin_headers()).json()["count"] == 0
    client.post(f"/tickets/{t['code']}/checkin", headers=admin_headers())
    data = client.get("/admin/checkins", headers=admin_headers()).json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["name"] == "Asha" and item["event"] == "Hackathon" and item["checked_in_at"].endswith("Z")


def test_team_checkin_shows_team_name():
    ev = make_event(min_team_size=2, max_team_size=4)
    t = student_post("/register", json=team_body(ev, team="Zeta", members=2)).json()
    out = client.post(f"/tickets/{t['code']}/checkin", headers=admin_headers()).json()
    assert out["team_name"] == "Zeta" and out["checked_in_at"]
    item = client.get("/admin/checkins", headers=admin_headers()).json()["items"][0]
    assert item["team_name"] == "Zeta" and item["team_size"] == 3


def test_team_reminders_email_every_member_with_email(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, sub, body: sent.append(to) or "sent")
    ev = make_event(min_team_size=2, max_team_size=4)
    body = team_body(ev, members=2)
    body["members"][1]["email"] = ""   # one member gave no email
    student_post("/register", json=body)
    r = client.post(f"/admin/events/{ev['id']}/remind", headers=admin_headers())
    assert r.json()["sent"] == 2 and sorted(sent) == ["lead@x.com", "m0@x.com"]


def test_migrate_adds_new_columns_to_old_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from app import database
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as c:
        c.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, name VARCHAR, capacity INTEGER)"))
        c.execute(text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, student_name VARCHAR)"))
        c.execute(text("INSERT INTO events (id, name, capacity) VALUES (1, 'Old event', 10)"))
    database.migrate(old)
    database.migrate(old)  # running twice must be safe
    insp = inspect(old)
    assert {"min_team_size", "max_team_size", "category"} <= {c["name"] for c in insp.get_columns("events")}
    assert {"team_name", "members", "checked_in_at", "status", "amount_paid", "user_id"} <= {c["name"] for c in insp.get_columns("tickets")}
    with old.begin() as c:
        assert c.execute(text("SELECT max_team_size FROM events")).scalar() == 1


# ───────────── accounts, cancel, waitlist, coupons, judging, analytics, certificate, metrics ─────────────

def signup(email="stu@x.com", name="Stu"):
    r = client.post("/auth/signup", json={"name": name, "email": email, "password": "secret1"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def student_post(path, **kwargs):
    """Call a student-only purchase route with a real student session."""
    payload = kwargs.get("json", {})
    email = payload.get("student_email") or payload.get("email") or "student@example.com"
    if "@" not in email or "." not in email.split("@")[-1]:
        email = "teststudent@example.com"
    name = payload.get("student_name") or payload.get("name") or "Test Student"
    if "headers" not in kwargs:
        created = client.post("/auth/signup", json={"name": name, "email": email, "password": "secret1"})
        if created.status_code == 409:
            logged = client.post("/auth/login", json={"username": email, "password": "secret1"})
            kwargs["headers"] = {"Authorization": f"Bearer {logged.json()['token']}"}
        else:
            kwargs["headers"] = {"Authorization": f"Bearer {created.json()['token']}"}
    return client.post(path, **kwargs)


def make_judge(email="j@x.com"):
    client.post("/admin/judges", json={"name": "Judge", "email": email, "password": "judgepw"},
                headers=admin_headers())
    r = client.post("/auth/login", json={"username": email, "password": "judgepw"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_signup_login_and_me():
    h = signup()
    assert client.get("/auth/me", headers=h).json()["role"] == "student"
    r = client.post("/auth/login", json={"username": "STU@x.com", "password": "secret1"})
    assert r.status_code == 200 and r.json()["role"] == "student"
    assert client.post("/auth/login", json={"username": "stu@x.com", "password": "wrong"}).status_code == 401


def test_signup_rejects_duplicates_and_weak_passwords():
    signup()
    dup = client.post("/auth/signup", json={"name": "S", "email": "stu@x.com", "password": "secret1"})
    assert dup.status_code == 409
    weak = client.post("/auth/signup", json={"name": "S", "email": "new@x.com", "password": "123"})
    assert weak.status_code == 422


def test_student_token_cannot_use_admin_routes():
    h = signup()
    assert client.get("/admin/stats", headers=h).status_code == 403
    assert client.post("/events", json={"name": "X"}, headers=h).status_code == 403


def test_my_tickets_lists_own_tickets_only():
    ev = make_event(capacity=5)
    h = signup("a@x.com")
    student_post("/register", json={"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com"}, headers=h)
    student_post("/register", json={"event_id": ev["id"], "student_name": "B", "student_email": "b@x.com"})
    mine = client.get("/my/tickets", headers=h).json()
    assert len(mine) == 1 and mine[0]["student_email"] == "a@x.com"
    assert client.get("/my/tickets").status_code == 401


def test_cancel_refunds_frees_seat_and_notifies_waitlist(monkeypatch):
    mails, refunds = [], []
    monkeypatch.setattr(mailer, "send_email", lambda to, sub, body: mails.append(to) or "sent")
    monkeypatch.setattr(payment_client, "refund", lambda amount, ref: refunds.append(amount) or {"status": "ok"})
    ev = make_event(capacity=1, price=100)
    h = signup("a@x.com")
    t = student_post("/register", json={"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com"},
                    headers=h).json()
    waitlist = student_post(f"/events/{ev['id']}/waitlist", json={"name": "W", "email": "w@x.com"})
    assert waitlist.status_code == 201 and waitlist.json()["position"] == 1
    r = client.post(f"/tickets/{t['code']}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["refunded"] == 100 and r.json()["waitlist_notified"] == "W"
    assert refunds == [100] and mails == ["w@x.com"]
    assert client.get("/events").json()[0]["seats_left"] == 1
    assert client.post(f"/tickets/{t['code']}/cancel", headers=h).status_code == 409


def test_cannot_cancel_someone_elses_ticket():
    ev = make_event(capacity=5)
    t = reg(ev, "A", "a@x.com")
    other = signup("other@x.com")
    assert client.post(f"/tickets/{t['code']}/cancel", headers=other).status_code == 403
    assert client.post(f"/tickets/{t['code']}/cancel").status_code == 401


def test_checked_in_cannot_be_cancelled_and_cancelled_cannot_check_in():
    ev = make_event(capacity=5, price=0)
    a, b = reg(ev, "A", "a@x.com"), reg(ev, "B", "b@x.com")
    client.post(f"/tickets/{a['code']}/checkin", headers=admin_headers())
    assert client.post(f"/tickets/{a['code']}/cancel", headers=admin_headers()).status_code == 409
    assert client.post(f"/tickets/{b['code']}/cancel", headers=admin_headers()).status_code == 200
    assert client.post(f"/tickets/{b['code']}/checkin", headers=admin_headers()).status_code == 409


def test_waitlist_only_when_full_and_no_duplicates():
    ev = make_event(capacity=1)
    body = {"name": "W", "email": "w@x.com"}
    assert student_post(f"/events/{ev['id']}/waitlist", json=body).status_code == 409
    reg(ev)
    assert student_post(f"/events/{ev['id']}/waitlist", json=body).status_code == 201
    assert student_post(f"/events/{ev['id']}/waitlist", json=body).status_code == 409


def test_coupon_discount_is_charged_and_counted(monkeypatch):
    charged = []
    monkeypatch.setattr(payment_client, "charge",
                        lambda amount, email: charged.append(amount) or {"status": "success", "reference": "P"})
    ev = make_event(price=200, capacity=5)
    client.post("/admin/coupons", json={"code": "half", "percent": 50, "max_uses": 1}, headers=admin_headers())
    v = client.post("/coupons/validate", json={"code": "HALF", "event_id": ev["id"]}).json()
    assert v["final_price"] == 100
    body = {"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com", "coupon_code": "half"}
    t = student_post("/register", json=body).json()
    assert charged == [100] and t["amount_paid"] == 100 and t["coupon_code"] == "HALF"
    assert student_post("/register", json=body).status_code == 400  # the single use is gone
    assert client.get("/admin/stats", headers=admin_headers()).json()["total_revenue"] == 100


def test_invalid_coupon_is_rejected():
    ev = make_event()
    body = {"event_id": ev["id"], "student_name": "A", "student_email": "a@x.com", "coupon_code": "NOPE"}
    assert student_post("/register", json=body).status_code == 400
    assert client.post("/admin/coupons", json={"code": "X", "percent": 10}, headers=admin_headers()).status_code == 422
    assert client.post("/admin/coupons", json={"code": "OKAY1", "percent": 0},
                       headers=admin_headers()).status_code == 422


def test_judging_and_leaderboard():
    ev = make_event(capacity=5, price=0, min_team_size=2, max_team_size=3)

    def team(name, leader):
        return student_post("/register", json={"event_id": ev["id"], "student_name": leader,
                                              "student_email": f"{leader}@x.com", "team_name": name,
                                              "members": [{"name": leader + "2"}]}).json()

    def score(h, code, n):
        return client.put("/judge/scores", headers=h, json={
            "code": code, "innovation": n, "technical": n, "presentation": n, "impact": n})

    a, b = team("Alpha", "a"), team("Beta", "b")
    j1, j2 = make_judge("j1@x.com"), make_judge("j2@x.com")
    assert score(j1, a["code"], 9).status_code == 200
    assert score(j2, a["code"], 7).status_code == 200
    assert score(j1, b["code"], 5).status_code == 200
    board = client.get(f"/events/{ev['id']}/leaderboard").json()
    assert [r["team"] for r in board["items"]] == ["Alpha", "Beta"]
    assert board["items"][0]["total"] == 32 and board["items"][0]["judges"] == 2 and board["items"][0]["rank"] == 1
    assert board["items"][1]["total"] == 20
    score(j1, b["code"], 10)  # re-scoring replaces the earlier score
    board = client.get(f"/events/{ev['id']}/leaderboard").json()
    assert board["items"][0]["team"] == "Beta" and board["items"][0]["total"] == 40


def test_only_judges_can_score_and_scores_are_limited_to_0_to_10():
    ev = make_event(price=0)
    t = reg(ev)
    body = {"code": t["code"], "innovation": 5, "technical": 5, "presentation": 5, "impact": 5}
    assert client.put("/judge/scores", json=body).status_code == 401
    assert client.put("/judge/scores", json=body, headers=signup()).status_code == 403
    assert client.put("/judge/scores", json=body, headers=admin_headers()).status_code == 403
    j = make_judge()
    assert client.put("/judge/scores", json={**body, "impact": 11}, headers=j).status_code == 422
    teams = client.get(f"/judge/events/{ev['id']}/teams", headers=j).json()
    assert teams["items"][0]["my_scores"] is None


def test_analytics_and_csv_export():
    ev = make_event(price=100, capacity=5)
    t = reg(ev, "=Evil", "a@x.com")
    client.post(f"/tickets/{t['code']}/checkin", headers=admin_headers())
    a = client.get("/admin/analytics", headers=admin_headers()).json()
    assert len(a["by_day"]) == 14 and a["by_day"][-1]["count"] == 1
    assert a["by_event"][0]["revenue"] == 100 and a["by_event"][0]["check_in_rate"] == 100
    text = client.get("/admin/export.csv", headers=admin_headers()).text
    assert t["code"] in text and "'=Evil" in text
    assert client.get("/admin/export.csv").status_code == 401


def test_certificate_needs_check_in_and_returns_pdf():
    ev = make_event(price=0, capacity=5, min_team_size=2, max_team_size=3)
    t = student_post("/register", json={"event_id": ev["id"], "student_name": "Asha", "student_email": "a@x.com",
                                       "team_name": "Alpha", "members": [{"name": "Ravi"}]}).json()
    assert client.get(f"/tickets/{t['code']}/certificate.pdf").status_code == 409
    client.post(f"/tickets/{t['code']}/checkin", headers=admin_headers())
    r = client.get(f"/tickets/{t['code']}/certificate.pdf?name=ravi")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and b"Ravi" in r.content
    assert client.get(f"/tickets/{t['code']}/certificate.pdf?name=Nobody").status_code == 404


def test_metrics_endpoint_exposes_counters():
    ev = make_event(price=0, capacity=5)
    reg(ev)
    client.get("/events")
    text = client.get("/metrics").text
    assert "fest_registrations_total 1" in text
    assert 'fest_http_requests_total{method="GET",path="/events"' in text


def test_event_category_and_poster():
    ev = make_event(category="Sports", image_url="https://example.com/p.png")
    assert ev["category"] == "Sports" and ev["image_url"].endswith("p.png")
    assert make_event(category="  ")["category"] == "General"


def test_reminders_skip_cancelled_tickets(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, sub, body: sent.append(to) or "sent")
    ev = make_event(price=0, capacity=5)
    a = reg(ev, "A", "a@x.com")
    reg(ev, "B", "b@x.com")
    client.post(f"/tickets/{a['code']}/cancel", headers=admin_headers())
    client.post(f"/admin/events/{ev['id']}/remind", headers=admin_headers())
    assert sent == ["b@x.com"]


# ───────────── seat allotment ─────────────

def seat_body(ev, name="A", email="a@x.com", **extra):
    return {"event_id": ev["id"], "student_name": name, "student_email": email, **extra}


def test_seated_event_shows_seat_map_and_books_chosen_seat():
    ev = make_event(price=0, capacity=100, seat_rows=3, seat_cols=4)
    info = client.get(f"/events/{ev['id']}/seats").json()
    assert info["seated"] and info["rows"] == 3 and info["cols"] == 4 and info["taken"] == []
    t = student_post("/register", json=seat_body(ev, seats=["b2"])).json()
    assert t["seats"] == ["B2"]
    assert client.get(f"/events/{ev['id']}/seats").json()["taken"] == ["B2"]
    assert client.get("/events").json()[0]["seats_left"] == 11


def test_same_seat_cannot_be_booked_twice():
    ev = make_event(price=0, capacity=100, seat_rows=2, seat_cols=2)
    assert student_post("/register", json=seat_body(ev, seats=["A1"])).status_code == 201
    again = student_post("/register", json=seat_body(ev, "B", "b@x.com", seats=["A1"]))
    assert again.status_code == 409 and "A1" in again.json()["detail"]
    assert len(client.get("/admin/stats", headers=admin_headers()).json()["events"]) == 1


def test_seats_are_assigned_automatically_when_none_chosen():
    ev = make_event(price=0, capacity=100, seat_rows=3, seat_cols=5)
    a = student_post("/register", json=seat_body(ev)).json()
    b = student_post("/register", json=seat_body(ev, "B", "b@x.com")).json()
    assert len(a["seats"]) == 1 and len(b["seats"]) == 1 and a["seats"] != b["seats"]
    assert a["seats"][0].startswith("B")  # the middle row is preferred


def test_invalid_seat_requests_are_rejected():
    ev = make_event(price=0, capacity=100, seat_rows=2, seat_cols=3)
    assert student_post("/register", json=seat_body(ev, seats=["Z9"])).status_code == 422
    assert student_post("/register", json=seat_body(ev, seats=["A1", "A2"])).status_code == 422  # needs exactly 1
    plain = make_event(price=0)
    assert student_post("/register", json=seat_body(plain, seats=["A1"])).status_code == 422
    assert client.post("/events", json={"name": "Bad", "seat_rows": 3}, headers=admin_headers()).status_code == 400


def test_team_needs_one_seat_per_member_and_gets_seats_together():
    ev = make_event(price=0, capacity=10, min_team_size=2, max_team_size=3, seat_rows=2, seat_cols=6)
    body = seat_body(ev, team_name="Alpha", members=[{"name": "Ravi"}, {"name": "Meena"}])
    wrong = student_post("/register", json={**body, "seats": ["A1", "A2"]})
    assert wrong.status_code == 422
    t = student_post("/register", json=body).json()
    assert len(t["seats"]) == 3
    row = {x[0] for x in t["seats"]}
    nums = sorted(int(x[1:]) for x in t["seats"])
    assert len(row) == 1 and nums == list(range(nums[0], nums[0] + 3))  # side by side


def test_cancelling_frees_the_seat():
    ev = make_event(price=0, capacity=100, seat_rows=1, seat_cols=2)
    t = student_post("/register", json=seat_body(ev, seats=["A1"])).json()
    assert client.post(f"/tickets/{t['code']}/cancel", headers=admin_headers()).status_code == 200
    assert client.get(f"/events/{ev['id']}/seats").json()["taken"] == []
    assert student_post("/register", json=seat_body(ev, "B", "b@x.com", seats=["A1"])).status_code == 201


def test_organizer_sees_seat_map_and_can_move_a_ticket():
    ev = make_event(price=0, capacity=100, seat_rows=2, seat_cols=3)
    t = student_post("/register", json=seat_body(ev, seats=["A1"])).json()
    other = student_post("/register", json=seat_body(ev, "B", "b@x.com", seats=["A2"])).json()
    m = client.get(f"/admin/events/{ev['id']}/seatmap", headers=admin_headers()).json()
    assert {x["seat"]: x["name"] for x in m["seats"]} == {"A1": "A", "A2": "B"}
    assert client.get(f"/admin/events/{ev['id']}/seatmap").status_code == 401
    moved = client.put(f"/admin/tickets/{t['code']}/seats", json={"seats": ["B3"]}, headers=admin_headers())
    assert moved.status_code == 200 and moved.json()["seats"] == ["B3"]
    taken = client.put(f"/admin/tickets/{t['code']}/seats", json={"seats": ["A2"]}, headers=admin_headers())
    assert taken.status_code == 409 and other["seats"] == ["A2"]
    same = client.put(f"/admin/tickets/{t['code']}/seats", json={"seats": ["B3"]}, headers=admin_headers())
    assert same.status_code == 200  # keeping your own seat is allowed


def test_check_in_shows_the_seat():
    ev = make_event(price=0, capacity=100, seat_rows=2, seat_cols=3)
    t = student_post("/register", json=seat_body(ev, seats=["B2"])).json()
    r = client.post(f"/tickets/{t['code']}/checkin", headers=admin_headers()).json()
    assert r["seats"] == ["B2"]
    assert client.get("/admin/checkins", headers=admin_headers()).json()["items"][0]["seats"] == ["B2"]


def test_deleting_a_seated_event_works():
    ev = make_event(price=0, capacity=100, seat_rows=2, seat_cols=3)
    student_post("/register", json=seat_body(ev))
    assert client.delete(f"/events/{ev['id']}", headers=admin_headers()).status_code == 204


# ───────────── food stalls and pre-orders ─────────────

def make_stall(name="Chai Stall", items=(("Tea", 20, None),)):
    sid = client.post("/admin/stalls", json={"name": name}, headers=admin_headers()).json()["id"]
    for n, price, stock in items:
        client.post(f"/admin/stalls/{sid}/items", json={"name": n, "price": price, "stock": stock},
                    headers=admin_headers())
    return client.get("/stalls").json()[-1]


def order_body(stall, qty=1, item=0, **extra):
    return {"name": "Asha", "email": "a@x.com", "stall_id": stall["id"],
            "items": [{"item_id": stall["items"][item]["id"], "qty": qty}], **extra}


def test_stalls_are_public_but_managing_them_is_not():
    stall = make_stall()
    assert stall["name"] == "Chai Stall" and stall["items"][0]["available"] is True
    assert client.post("/admin/stalls", json={"name": "X"}).status_code == 401
    assert client.post("/admin/stalls", json={"name": "X"}, headers=signup()).status_code == 403
    assert client.put("/admin/orders/FABCDE/status", json={"status": "ready"}).status_code == 401


def test_placing_an_order_charges_the_total(monkeypatch):
    charged = []
    monkeypatch.setattr(payment_client, "charge",
                        lambda amount, email: charged.append(amount) or {"status": "success", "reference": "P1"})
    stall = make_stall(items=(("Tea", 20, None), ("Samosa", 25, None)))
    body = {"name": "Asha", "email": "a@x.com", "stall_id": stall["id"], "note": "less sugar",
            "items": [{"item_id": stall["items"][0]["id"], "qty": 2}, {"item_id": stall["items"][1]["id"], "qty": 1}]}
    o = student_post("/orders", json=body).json()
    assert charged == [65] and o["total"] == 65 and o["status"] == "placed" and o["code"].startswith("F")
    assert [i["qty"] for i in o["items"]] == [2, 1]
    assert client.get(f"/orders/{o['code'].lower()}").json()["code"] == o["code"]
    assert client.get(f"/orders/{o['code']}/qr.png").content.startswith(b"\x89PNG")
    assert client.get("/orders/FNOPE1").status_code == 404


def test_order_validation():
    stall = make_stall()
    other = make_stall("Juice", (("Mango", 60, None),))
    assert student_post("/orders", json={**order_body(stall), "items": []}).status_code == 422
    assert student_post("/orders", json=order_body(stall, qty=11)).status_code == 422
    # Identity fields are taken from the signed-in account, not the request body.
    assert student_post("/orders", json={**order_body(stall), "email": "nope"}).status_code == 201
    wrong_menu = {**order_body(stall), "items": [{"item_id": other["items"][0]["id"], "qty": 1}]}
    assert student_post("/orders", json=wrong_menu).status_code == 400
    client.put(f"/admin/stalls/{stall['id']}", json={"open": False}, headers=admin_headers())
    assert student_post("/orders", json=order_body(stall)).status_code == 409


def test_stock_runs_out_and_cancelling_gives_it_back():
    stall = make_stall(items=(("Biryani", 100, 2),))
    first = student_post("/orders", json=order_body(stall, qty=2))
    assert first.status_code == 201
    assert client.get("/stalls").json()[-1]["items"][0]["available"] is False
    assert student_post("/orders", json=order_body(stall)).status_code == 409
    cancelled = client.post(f"/orders/{first.json()['code']}/cancel?email=a@x.com")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.get("/stalls").json()[-1]["items"][0]["stock"] == 2
    assert student_post("/orders", json=order_body(stall, qty=3)).status_code == 409  # only 2 in stock


def test_order_status_flow_and_ready_email(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, sub, body: sent.append((to, sub)) or "sent")
    stall = make_stall()
    o = student_post("/orders", json=order_body(stall)).json()

    def move(status):
        return client.put(f"/admin/orders/{o['code']}/status", json={"status": status}, headers=admin_headers())

    assert move("ready").status_code == 409  # cannot skip preparing
    assert move("preparing").json()["status"] == "preparing"
    assert move("ready").json()["status"] == "ready" and sent and sent[0][0] == "a@x.com"
    assert move("collected").json()["status"] == "collected"
    assert move("preparing").status_code == 409
    active = client.get("/admin/orders?active=true", headers=admin_headers()).json()
    assert active == []
    assert len(client.get("/admin/orders", headers=admin_headers()).json()) == 1


def test_cancel_rules_for_orders():
    stall = make_stall(items=(("Tea", 20, None),))
    o = student_post("/orders", json=order_body(stall)).json()
    assert client.post(f"/orders/{o['code']}/cancel").status_code == 403
    assert client.post(f"/orders/{o['code']}/cancel?email=someone@else.com").status_code == 403
    client.put(f"/admin/orders/{o['code']}/status", json={"status": "preparing"}, headers=admin_headers())
    assert client.post(f"/orders/{o['code']}/cancel?email=a@x.com").status_code == 409
    cancelled = client.put(f"/admin/orders/{o['code']}/status", json={"status": "cancelled"},
                           headers=admin_headers())
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_order_refund_goes_to_the_payment_service(monkeypatch):
    refunds = []
    monkeypatch.setattr(payment_client, "refund", lambda amount, ref: refunds.append((amount, ref)) or {})
    stall = make_stall(items=(("Tea", 20, None),))
    o = student_post("/orders", json=order_body(stall, qty=2)).json()
    client.post(f"/orders/{o['code']}/cancel?email=a@x.com")
    assert refunds == [(40, "PAY-TEST")]


def test_order_linked_to_a_ticket_shows_the_seat():
    ev = make_event(price=0, capacity=100, seat_rows=2, seat_cols=3)
    t = student_post("/register", json=seat_body(ev, seats=["B2"])).json()
    stall = make_stall()
    o = student_post("/orders", json=order_body(stall, ticket_code=t["code"])).json()
    assert o["seats"] == ["B2"]
    assert student_post("/orders", json=order_body(stall, ticket_code="NOPE")).status_code == 404


def test_my_orders_lists_only_my_orders():
    stall = make_stall()
    h = signup("a@x.com")
    student_post("/orders", json=order_body(stall), headers=h)
    student_post("/orders", json={**order_body(stall), "email": "b@x.com"})
    mine = client.get("/my/orders", headers=h).json()
    assert len(mine) == 1 and mine[0]["email"] == "a@x.com"
    assert client.get("/my/orders").status_code == 401


def test_stall_with_orders_cannot_be_deleted_but_items_can():
    stall = make_stall(items=(("Tea", 20, None), ("Coffee", 30, None)))
    student_post("/orders", json=order_body(stall))
    assert client.delete(f"/admin/stalls/{stall['id']}", headers=admin_headers()).status_code == 409
    assert client.delete(f"/admin/items/{stall['items'][1]['id']}", headers=admin_headers()).status_code == 204
    toggled = client.put(f"/admin/items/{stall['items'][0]['id']}", json={"available": False},
                         headers=admin_headers()).json()
    assert toggled["items"][0]["available"] is False


def test_food_numbers_in_dashboard_and_metrics():
    stall = make_stall(items=(("Tea", 20, None),))
    student_post("/orders", json=order_body(stall, qty=3))
    s = client.get("/admin/stats", headers=admin_headers()).json()
    assert s["food_orders"] == 1 and s["food_revenue"] == 60
    text = client.get("/metrics").text
    assert "fest_food_orders_total 1" in text and "fest_food_revenue_total 60" in text


def test_event_registration_requires_student_login_and_uses_account_identity():
    ev = make_event(price=0)
    body = {"event_id": ev["id"], "student_name": "Impersonated Name", "student_email": "other@example.com"}
    assert client.post("/register", json=body).status_code == 401
    h = signup("realstudent@example.com", "Real Student")
    response = client.post("/register", json=body, headers=h)
    assert response.status_code == 201
    assert response.json()["student_name"] == "Real Student"
    assert response.json()["student_email"] == "realstudent@example.com"


def test_student_purchase_endpoints_require_login():
    assert client.post("/payments/order", json={"amount": 100, "email": "x@example.com"}).status_code == 401
    assert client.post("/orders", json={"name": "X", "email": "x@example.com", "stall_id": 1,
                                         "items": [{"item_id": 1, "qty": 1}]}).status_code == 401


def test_student_profile_wishlist_feedback_and_notifications():
    headers = signup("features@example.com", "Feature Student")
    event = make_event(name="Feature Event")
    assert client.put("/auth/profile", json={"name": "Updated Student"}, headers=headers).json()["name"] == "Updated Student"
    assert client.post(f"/my/wishlist/{event['id']}", headers=headers).status_code == 201
    assert client.get("/my/wishlist", headers=headers).json()[0]["event_id"] == event["id"]
    assert client.post("/my/feedback", json={"event_id": event["id"], "rating": 5, "comment": "Great"}, headers=headers).status_code == 201
    admin = admin_headers()
    assert client.post("/admin/announcements", json={"title": "Fest update", "message": "Schedule published"}, headers=admin).status_code == 201
    notices = client.get("/my/notifications", headers=headers).json()
    assert notices and notices[0]["title"] == "Fest update"
    assert client.get("/admin/feedback", headers=admin).json()[0]["rating"] == 5
