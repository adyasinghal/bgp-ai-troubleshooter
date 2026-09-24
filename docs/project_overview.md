# Real-Time AI Network Troubleshooter for BGP Switches
**HPE CPP-2 Program Documentation**

## 1. Architecture & How It Works

### Work Flow
1. A user notices an anomaly on a BGP device.
2. The user attaches the tool (via SSH, telnet, or any other means) to the switch.
3. Run our tool and follows the recommendation given by the tool to solve the anomaly.

### Phases
- **Phase 1:** The anomaly is a configuration issue on the switch.
- **Phase 2:** Detect anomaly based on event logs and support logs.
- **Phase 3:** Tool collects periodic data and suggests fixes in real-time.

### Tool Design (High Level)
- **Connect:** Log in to switch/router via SSH (`Netmiko` / `Paramiko`) or REST API.
- **Collect:** Run BGP health commands (summary, neighbors, route) based on user input via tool calling.
- **Parse:** Structure raw CLI output into JSON using `TextFSM` / regex.
- **Analyze:** Rule engine + ML engine (`scikit-learn`) detects BGP faults.
- **Diagnose:** Map each fault to a probable cause and a suggested fix.
- **Alert:** Show real-time status and alerts on a Flask / Streamlit dashboard.

### Pipeline
`Switch/Router` -> `SSH/API Collector (Tools Calling)` -> `TextFSM Parser` -> `AI Analyzer` -> `Dashboard / Alerts`

---

## 2. Development Tools & Runtime Environment

### Language
- Python 3.x

### Networking Tools & Lab
- FRRouting, Containerlab, open-vswitch (OVS)
- Real switch/router or simulators: Cisco, Arista, Juniper, SONiC

### Python Libraries
- **Netmiko / Paramiko:** SSH automation
- **TextFSM:** CLI parsing
- **Pandas:** Data handling

### AI & UI
- **Scikit-learn:** Diagnostics & anomaly detection
- **Flask / Streamlit:** Dashboard interface

---

## 3. Students' Knowledge (Prerequisites)

### Networking
- IP addressing & routing basics
- BGP fundamentals (`eBGP` / `iBGP`)
- Basic switch / router CLI commands

### Programming
- Python basics
- SSH automation scripts
- JSON / Regex parsing

### Helpful Extras
- Linux basics
- REST APIs
- Flask / Streamlit 

---

## 4. Expected Outcomes

### Real-Time Features
- Connects to switch / router live
- Runs BGP health commands based on user input
- Detects faults automatically
- Shows active alerts on a dashboard

### Final Deliverables
- Python source code 
- Dashboard / CLI tool
- Demonstration with live switch or lab 
- Project report

---

## 5. CPP-2 Program High Level Plan (Timeline)

### May / Jun
- Heads up to the colleges
- Program Kick-off
- Finalizing the students
- Mentors and projects nominations
- Kick start the projects allocation
- Initial Ramp up

### Jul / Aug
- Check point review
- Technical sessions
- Internal Demos
- Mentors and champs sync up
- Industry visits
- Hackathon

### Sep / Oct
- Check point
- Technical sessions
- Internal Demos
- Mentors and champs sync up
- Industry visits
- Hackations
- Technical sessions

### Nov / Dec
- Final Demos
- Closure of the projects
- Documentation
- Evaluation for CPP-3
- Initiate CPP3 process

### HPE Technologies / Process and Culture Aspects
- Branding activities, technical sessions, and various engagements continued throughout the journey.
- Regular cadence meetings between HPE mentors, university mentors, and students.
- Consistent sync-ups with champs, core team, and mentors to ensure alignment.
- Students enable to contribute to different technical forums.


