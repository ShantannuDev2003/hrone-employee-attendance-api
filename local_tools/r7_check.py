import json, urllib.request, urllib.error

BASE = "http://localhost:8000"

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

# (emp_code, joined_on, expected working_days in 2026-10)
cases = [
    ("EMP9301", "2026-10-01", 22),  # Thursday, start of month
    ("EMP9302", "2026-10-05", 20),  # Monday
    ("EMP9303", "2026-10-10", 15),  # Saturday -> first working day is Mon 12
    ("EMP9304", "2026-10-11", 15),  # Sunday -> first working day is Mon 12
    ("EMP9305", "2026-10-30", 1),   # Friday, last working day of month
    ("EMP9306", "2026-10-31", 0),   # Saturday, last day of month
    ("EMP9307", "2026-11-02", 0),   # joins after the month
]

for code, joined, expected in cases:
    call("POST", "/employees", {
        "emp_code": code, "name": "R7 " + code,
        "email": code.lower() + "@example.com", "department": "R7TEST",
        "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined})
    status, body = call("GET", f"/analytics/employees/{code}/monthly?month=2026-10")
    got = body.get("working_days")
    pct = body.get("attendance_pct")
    ok = status == 200 and got == expected and (pct is None) == (expected == 0)
    print(("PASS" if ok else "FAIL"), code, "joined", joined,
          "expected", expected, "got", got, "pct", pct)

status, body = call("GET", "/analytics/employees/EMP0002/monthly?month=2026-07")
print("EMP0002 July working_days:", body.get("working_days"), "(expect 13 if joined_on is 2026-07-15)")