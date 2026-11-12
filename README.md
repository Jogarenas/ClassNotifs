# ClassNotifs

I built this so I don’t have to keep refreshing Davidson’s course schedule to
see if a seat opens up. It checks the classes I’m interested in and sends a
notification to my phone when there’s room.

## How it works

1. cron-job.org triggers a GitHub Actions workflow every minute. 
2. `monitor.py` gets the latest enrollment numbers from Davidson’s course API
   and checks every section of the courses on my list.
3. If a section has an open seat, it sends a push through [ntfy](https://ntfy.sh)
   with the course, available seats, instructor, meeting times, and schedule link.
4. It saves what it has already reported on the `monitor-state` branch so it
   doesn’t keep sending the same alert. If a section fills up and later reopens,
   it sends another notification.


## Choose classes

Edit `WATCHED_COURSES` near the top of [monitor.py](monitor.py). For example,
this is my current list for Spring 2027 (`TERM = "202602"`):

```python
WATCHED_COURSES = {
    "ECO": {"203", "204", "211"},
    "HIS": {"163"},
}
```

Add a department and its course numbers to watch something else. Every section
is checked automatically. 


