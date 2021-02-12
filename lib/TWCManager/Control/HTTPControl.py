import mimetypes
import os
import pathlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from termcolor import colored
from datetime import datetime, timedelta
import jinja2
import json
import re
import threading
import time
import urllib.parse
import math
from ww import f


class ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    pass


class HTTPControl:
    configConfig = {}
    configHTTP = {}
    httpPort = 8080
    master = None
    status = False

    def __init__(self, master):

        self.master = master
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}
        try:
            self.configHTTP = master.config["control"]["HTTP"]
        except KeyError:
            self.configHTTP = {}
        self.httpPort = self.configHTTP.get("listenPort", 8080)
        self.status = self.configHTTP.get("enabled", False)

        # Unload if this module is disabled or misconfigured
        if (not self.status) or (int(self.httpPort) < 1):
            self.master.releaseModule("lib.TWCManager.Control", "HTTPControl")
            return None

        HTTPHandler = CreateHTTPHandlerClass(master)
        httpd = ThreadingSimpleServer(("", self.httpPort), HTTPHandler)
        self.master.debugLog(1, "HTTPCtrl", "Serving at port: " + str(self.httpPort))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()


def CreateHTTPHandlerClass(master):
  class HTTPControlHandler(BaseHTTPRequestHandler):
    ampsList = []
    fields = {}
    hoursDurationList = []
    master = None
    path = ""
    post_data = ""
    templateEnv = None
    templateLoader = None
    timeList = []
    url = None

    def __init__(self, *args, **kwargs):

        # Populate ampsList so that any function which requires a list of supported
        # TWC amps can easily access it
        if not len(self.ampsList):
            self.ampsList.append([0, "Disabled"])
            for amp in range(5, (master.config["config"].get("wiringMaxAmpsPerTWC", 5)) + 1):
                self.ampsList.append([amp, str(amp) + "A"])

        # Populate list of hours
        if not len(self.hoursDurationList):
            for hour in range(1, 25):
                self.hoursDurationList.append([(hour * 3600), str(hour) + "h"])

        if not len(self.timeList):
            for hour in range(0, 24):
                for mins in [0, 15, 30, 45]:
                    strHour = str(hour)
                    strMins = str(mins)
                    if hour < 10:
                        strHour = "0" + str(hour)
                    if mins < 10:
                        strMins = "0" + str(mins)
                    self.timeList.append([strHour + ":" + strMins, strHour + ":" + strMins])

        # Define jinja2 template environment
        # Note that we specify two paths in order to the template loader.
        # The first is the user specified template. The second is the default.
        # Jinja2 will try for the specified template first, however if any files
        # are not found, it will fall back to the default theme.
        self.templateLoader = jinja2.FileSystemLoader(searchpath=[
          pathlib.Path(__file__).resolve().parent.as_posix()+"/themes/" + master.settings.get("webControlTheme", "Default")+"/",
          pathlib.Path(__file__).resolve().parent.as_posix()+"/themes/Default/"])
        self.templateEnv = jinja2.Environment(loader=self.templateLoader, autoescape=True)

        # Make certain functions available to jinja2
        # Where we have helper functions that we've used in the fast to
        # render HTML, we can keep using those even inside jinja2
        self.templateEnv.globals.update(addButton=self.addButton)
        self.templateEnv.globals.update(ampsList=self.ampsList)
        self.templateEnv.globals.update(chargeScheduleDay=self.chargeScheduleDay)
        self.templateEnv.globals.update(doChargeSchedule=self.do_chargeSchedule)
        self.templateEnv.globals.update(hoursDurationList=self.hoursDurationList)
        self.templateEnv.globals.update(navbarItem=self.navbar_item)
        self.templateEnv.globals.update(optionList=self.optionList)
        self.templateEnv.globals.update(showTWCs=self.show_twcs)
        self.templateEnv.globals.update(timeList=self.timeList)

        # Set master object
        self.master = master

        # Call parent constructor last, this is where the request is served
        BaseHTTPRequestHandler.__init__(self, *args, **kwargs)

    def checkBox(self, name, value):
        cb = "<input type=checkbox name='" + name + "'"
        if value:
            cb += " checked"
        cb += ">"
        return cb

    def do_chargeSchedule(self):
        schedule = [ "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday" ]
        settings = master.settings.get("Schedule", {})

        page = """
    <table class='table table-sm'>
      <thead>
        <th scope='col'>&nbsp;</th>
        """
        for day in schedule:
            page += "<th scope='col'>" + day[:3] + "</th>"
        page += """
      </thead>
      <tbody>"""
        for i in (x for y in (range(6, 24), range(0, 6)) for x in y):
            page += "<tr><th scope='row'>%02d</th>" % (i)
            for day in schedule:
                today = settings.get(day, {})
                curday = settings.get("Common", {})
                if (settings.get("schedulePerDay", 0)):
                    curday = settings.get(day, {})
                if (today.get("enabled", None) == "on" and
                   (int(curday.get("start", 0)[:2]) <= int(i)) and
                   (int(curday.get("end", 0)[:2]) > int(i))):
                     page += "<td bgcolor='#CFFAFF'>SC @ " + str(settings.get("Settings", {}).get("scheduledAmpsMax", 0)) + "A</td>"
                else:
                    #Todo - need to mark track green + non scheduled chg
                    page += "<td bgcolor='#FFDDFF'>&nbsp;</td>"
            page += "</tr>"
        page += "</tbody>"
        page += "</table>"

        return page

    def navbar_item(self, url, name):
        active = ""
        urlp = urllib.parse.urlparse(self.path)
        if urlp.path == url:
            active = "active"
        page = "<li class='nav-item %s'>" % active
        page += "<a class='nav-link' href='%s'>%s</a>" % (url, name)
        page += "</li>"
        return page

    def do_API_GET(self):
        self.debugLogAPI("Starting API GET")
        if self.url.path == "/api/getConfig":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            json_data = json.dumps(master.config)
            # Scrub output of passwords and API keys
            json_datas = re.sub(r'"password": ".*?",', "", json_data)
            json_data = re.sub(r'"apiKey": ".*?",', "", json_datas)
            self.wfile.write(json_data.encode("utf-8"))

        elif self.url.path == "/api/getPolicy":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            json_data = json.dumps(
                master.getModuleByName("Policy").charge_policy
            )
            self.wfile.write(json_data.encode("utf-8"))

        elif self.url.path == "/api/getSlaveTWCs":
            data = {}
            totals = {
                "lastAmpsOffered": 0,
                "lifetimekWh": 0,
                "maxAmps": 0,
                "reportedAmpsActual": 0,
            }
            for slaveTWC in master.getSlaveTWCs():
                TWCID = "%02X%02X" % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
                data[TWCID] = {
                    "currentVIN": slaveTWC.currentVIN,
                    "lastAmpsOffered": round(slaveTWC.lastAmpsOffered, 2),
                    "lastHeartbeat": round(time.time() - slaveTWC.timeLastRx, 2),
                    "lastVIN": slaveTWC.lastVIN,
                    "lifetimekWh": slaveTWC.lifetimekWh,
                    "maxAmps": float(slaveTWC.maxAmps),
                    "reportedAmpsActual": float(slaveTWC.reportedAmpsActual),
                    "state": slaveTWC.reportedState,
                    "version": slaveTWC.protocolVersion,
                    "voltsPhaseA": slaveTWC.voltsPhaseA,
                    "voltsPhaseB": slaveTWC.voltsPhaseB,
                    "voltsPhaseC": slaveTWC.voltsPhaseC,
                    "TWCID": "%s" % TWCID,
                }
                # Adding some vehicle data
                vehicle = slaveTWC.getLastVehicle()
                if vehicle != None:
                    data[TWCID]["lastBatterySOC"] = vehicle.batteryLevel
                    data[TWCID]["lastChargeLimit"] = vehicle.chargeLimit
                    data[TWCID]["lastAtHome"] = vehicle.atHome
                    data[TWCID]["lastTimeToFullCharge"] = vehicle.timeToFullCharge

                totals["lastAmpsOffered"] += slaveTWC.lastAmpsOffered
                totals["lifetimekWh"] += slaveTWC.lifetimekWh
                totals["maxAmps"] += slaveTWC.maxAmps
                totals["reportedAmpsActual"] += slaveTWC.reportedAmpsActual

            data["total"] = {
                "lastAmpsOffered": round(totals["lastAmpsOffered"], 2),
                "lifetimekWh": totals["lifetimekWh"],
                "maxAmps": totals["maxAmps"],
                "reportedAmpsActual": round(totals["reportedAmpsActual"], 2),
                "TWCID": "total",
            }

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            json_data = json.dumps(data)
            self.wfile.write(json_data.encode("utf-8"))

        elif self.url.path == "/api/getStatus":
            data = master.getStatus()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            json_data = json.dumps(data)
            try:
                self.wfile.write(json_data.encode("utf-8"))
            except BrokenPipeError:
                self.debugLogAPI("Connection Error: Broken Pipe")

        elif self.url.path == "/api/getHistory":
            output = []
            now = datetime.now().replace(second=0, microsecond=0).astimezone()
            startTime = now - timedelta(days=2) + timedelta(minutes=5)
            endTime = now.replace(minute=math.floor(now.minute / 5) * 5)
            startTime = startTime.replace(minute=math.floor(startTime.minute / 5) * 5)

            source = (
                master.settings["history"]
                if "history" in master.settings
                else []
            )
            data = {k: v for k, v in source if datetime.fromisoformat(k) >= startTime}

            avgCurrent = 0
            for slave in master.getSlaveTWCs():
                avgCurrent += slave.historyAvgAmps
            data[
                endTime.isoformat(timespec="seconds")
            ] = master.convertAmpsToWatts(avgCurrent)

            output = [
                {
                    "timestamp": timestamp,
                    "charger_power": data[timestamp] if timestamp in data else 0,
                }
                for timestamp in [
                    (startTime + timedelta(minutes=5 * i)).isoformat(timespec="seconds")
                    for i in range(48 * 12)
                ]
            ]

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            json_data = json.dumps(output)
            self.wfile.write(json_data.encode("utf-8"))

        else:
            # All other routes missed, return 404
            self.send_response(404)
            self.end_headers()
            self.wfile.write("".encode("utf-8"))

        self.debugLogAPI("Ending API GET")

    def do_API_POST(self):

        self.debugLogAPI("Starting API POST")

        if self.url.path == "/api/chargeNow":
            data = json.loads(self.post_data.decode("UTF-8"))
            rate = int(data.get("chargeNowRate", 0))
            durn = int(data.get("chargeNowDuration", 0))

            if rate == 0 or durn == 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write("".encode("utf-8"))

            else:
                master.setChargeNowAmps(rate)
                master.setChargeNowTimeEnd(durn)
                master.queue_background_task({"cmd": "saveSettings"})
                self.send_response(202)
                self.end_headers()
                self.wfile.write("".encode("utf-8"))

        elif self.url.path == "/api/cancelChargeNow":
            master.resetChargeNowAmps()
            master.queue_background_task({"cmd": "saveSettings"})
            self.send_response(202)
            self.end_headers()
            self.wfile.write("".encode("utf-8"))

        elif self.url.path == "/api/sendStartCommand":
            master.sendStartCommand()
            self.send_response(204)
            self.end_headers()

        elif self.url.path == "/api/sendStopCommand":
            master.sendStopCommand()
            self.send_response(204)
            self.end_headers()

        elif self.url.path == "/api/checkArrival":
            master.queue_background_task({"cmd": "checkArrival"})
            self.send_response(202)
            self.end_headers()
            self.wfile.write("".encode("utf-8"))

        elif self.url.path == "/api/checkDeparture":
            master.queue_background_task({"cmd": "checkDeparture"})
            self.send_response(202)
            self.end_headers()
            self.wfile.write("".encode("utf-8"))

        elif self.url.path == "/api/setScheduledChargingSettings":
            data = json.loads(self.post_data.decode("UTF-8"))
            enabled = bool(data.get("enabled", False))
            startingMinute = int(data.get("startingMinute", -1))
            endingMinute = int(data.get("endingMinute", -1))
            monday = bool(data.get("monday", False))
            tuesday = bool(data.get("tuesday", False))
            wednesday = bool(data.get("wednesday", False))
            thursday = bool(data.get("thursday", False))
            friday = bool(data.get("friday", False))
            saturday = bool(data.get("saturday", False))
            sunday = bool(data.get("sunday", False))
            amps = int(data.get("amps", -1))
            batterySize = int(
                data.get("flexBatterySize", 100)
            )  # using 100 as default, because with this every available car at moment should be finished with charging at the ending time
            flexStart = int(data.get("flexStartEnabled", False))
            weekDaysBitmap = (
                    (1 if monday else 0)
                    + (2 if tuesday else 0)
                    + (4 if wednesday else 0)
                    + (8 if thursday else 0)
                    + (16 if friday else 0)
                    + (32 if saturday else 0)
                    + (64 if sunday else 0)
            )

            if (
                    not (enabled)
                    or startingMinute < 0
                    or endingMinute < 0
                    or amps <= 0
                    or weekDaysBitmap == 0
            ):
                master.setScheduledAmpsMax(0)
                master.setScheduledAmpsStartHour(-1)
                master.setScheduledAmpsEndHour(-1)
                master.setScheduledAmpsDaysBitmap(0)
            else:
                master.setScheduledAmpsMax(amps)
                master.setScheduledAmpsStartHour(startingMinute / 60)
                master.setScheduledAmpsEndHour(endingMinute / 60)
                master.setScheduledAmpsDaysBitmap(weekDaysBitmap)
            master.setScheduledAmpsBatterySize(batterySize)
            master.setScheduledAmpsFlexStart(flexStart)
            master.queue_background_task({"cmd": "saveSettings"})
            self.send_response(202)
            self.end_headers()
            self.wfile.write("".encode("utf-8"))

        else:
            # All other routes missed, return 404
            self.send_response(404)
            self.end_headers()
            self.wfile.write("".encode("utf-8"))

        self.debugLogAPI("Ending API POST")

    def do_get_policy(self):
        page = """
      <table>
        """
        j = 0
        mod_policy = master.getModuleByName("Policy")
        insertion_points = {
            0: "Emergency",
            1: "Before",
            3: "After"
        }
        replaced = all(x not in mod_policy.default_policy for x in mod_policy.charge_policy)
        for policy in mod_policy.charge_policy:
            if policy in mod_policy.default_policy:
                cat = "Default"
                ext = insertion_points.get(j, None)

                if ext:
                    page += "<tr><th>Policy Extension Point</th></tr>"
                    page += "<tr><td>" + ext + "</td></tr>"

                j += 1
            else:
                cat = "Custom" if replaced else insertion_points.get(j, "Unknown")
            page += "<tr><td>&nbsp;</td><td>" + policy["name"] + " (" + cat + ")</td></tr>"
            page += "<tr><th>&nbsp;</th><th>&nbsp;</th><th>Match Criteria</th><th>Condition</th><th>Value</th></tr>"
            for match, condition, value in zip(policy["match"], policy["condition"], policy["value"]):
                page += "<tr><td>&nbsp;</td><td>&nbsp;</td>"
                page += "<td>" + str(match)
                match_result = mod_policy.policyValue(match)
                if match != match_result:
                    page += " (" + str(match_result) + ")"
                page += "</td>"

                page += "<td>" + condition + "</td>"

                page += "<td>" + str(value)
                value_result = mod_policy.policyValue(value)
                if value != value_result:
                    page += " (" + str(value_result) + ")"
                page += "</td></tr>"

        page += """
      </table>
      </div>
    </body>
        """
        return page

    def do_GET(self):
        self.url = urllib.parse.urlparse(self.path)

        # serve local static content files (from './lib/TWCManager/Control/static/' dir)
        if self.url.path.startswith('/static/'):
            content_type = mimetypes.guess_type(self.url.path)[0]

            # only server know content type
            if content_type is not None:
                filename = pathlib.Path(__file__).resolve().parent.as_posix() + self.url.path

                # check if static file exists and is readable
                if os.path.isfile(filename) and os.access(filename, os.R_OK):
                    self.send_response(200)
                    self.send_header('Content-type', content_type)
                    self.end_headers()

                    # send static content (e.g. images) to browser
                    with open(filename, 'rb') as staticFile:
                        self.wfile.write(staticFile.read())
                        return
                else:
                    # static file doesn't exit or isn't readable
                    self.send_response(404)
                    return

        # Service API requests
        if self.url.path.startswith("/api/"):
            self.do_API_GET()
            return

        if self.url.path == "/teslaAccount/login":
            # For security, these details should be submitted via a POST request
            # Send a 405 Method Not Allowed in response.
            self.send_response(405)
            page = "This function may only be requested via the POST HTTP method."
            self.wfile.write(page.encode("utf-8"))
            return

        if (
            self.url.path == "/"
            or self.url.path.startswith("/teslaAccount")
        ):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            # Load "main" template and render
            self.template = self.templateEnv.get_template("main.html.j2")

            # Set some values that we use within the template
            # Check if we're able to access the Tesla API
            self.apiAvailable = master.getModuleByName(
                "TeslaAPI"
            ).car_api_available()
            self.scheduledAmpsMax = master.getScheduledAmpsMax()

            # Send the html message
            page = self.template.render(vars(self))

            self.wfile.write(page.encode("utf-8"))
            return

        if self.url.path == "/debug":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            # Load debug template and render
            self.template = self.templateEnv.get_template("debug.html.j2")
            page = self.template.render(self.__dict__)

            self.wfile.write(page.encode("utf-8"))
            return

        if self.url.path == "/policy":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            # Load policy template and render
            self.template = self.templateEnv.get_template("policy.html.j2")
            page = self.template.render(self.__dict__)

            page += self.do_get_policy()
            self.wfile.write(page.encode("utf-8"))
            return

        if self.url.path == "/schedule":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            # Load template and render
            self.template = self.templateEnv.get_template("schedule.html.j2")
            page = self.template.render(self.__dict__)

            self.wfile.write(page.encode("utf-8"))
            return

        if self.url.path == "/settings":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            # Load template and render
            self.template = self.templateEnv.get_template("settings.html.j2")
            page = self.template.render(self.__dict__)

            self.wfile.write(page.encode("utf-8"))
            return

        # All other routes missed, return 404
        self.send_response(404)

    def do_POST(self):

        # Parse URL
        self.url = urllib.parse.urlparse(self.path)

        # Parse POST parameters
        self.fields.clear()
        length = int(self.headers.get("content-length"))
        self.post_data = self.rfile.read(length)

        if self.url.path.startswith("/api/"):
            self.do_API_POST()
            return

        self.fields = urllib.parse.parse_qs(self.post_data.decode("utf-8"))

        if self.url.path == "/schedule/save":
            # User has submitted schedule.
            self.process_save_schedule()
            return

        if self.url.path == "/settings/save":
            # User has submitted settings.
            # Call dedicated function
            self.process_save_settings()
            return

        if self.url.path == "/teslaAccount/login":
            # User has submitted Tesla login.
            # Pass it to the dedicated process_teslalogin function
            self.process_teslalogin()
            return

        # All other routes missed, return 404
        self.send_response(404)
        self.end_headers()
        self.wfile.write("".encode("utf-8"))
        return

    def addButton(self, button_def, extrargs):
        # This is a macro which can display differing buttons based on a
        # condition. It's a useful way to switch the text on a button based
        # on current state.
        page = "<input type='Submit' %s id='%s' value='%s'>" % (
            extrargs,
            button_def[0],
            button_def[1],
        )
        return page

    def chargeScheduleDay(self, day):

        # Fetch current settings
        sched = master.settings.get("Schedule", {})
        today = sched.get(day, {})
        suffix = day + "ChargeTime"

        # Render daily schedule options
        page  = "<tr>"
        page += "<td>" + self.checkBox("enabled"+suffix, 
                today.get("enabled", 0)) + "</td>"
        page += "<td>" + str(day) + "</td>"
        page += "<td>" + self.optionList(self.timeList, 
          {"name": "start"+suffix,
           "value": today.get("start", "00:00")}) + "</td>"
        page += "<td> to </td>"
        page += "<td>" + self.optionList(self.timeList, 
          {"name": "end"+suffix,
           "value": today.get("end", "00:00")}) + "</td>"
        page += "<td>" + self.checkBox("flex"+suffix, 
                today.get("flex", 0)) + "</td>"
        page += "<td>Flex Charge</td>"
        page += "</tr>"
        return page

    def getFieldValue(self, key):
        # Parse the form value represented by key, and return the
        # value either as an integer or string
        keya = str(key)
        vala = self.fields[key][0].replace("'", "")
        try:
            if int(vala) or vala == "0":
                return int(vala)
        except ValueError:
            return vala

    def log_message(self, format, *args):
        pass

    def optionList(self, list, opts={}):
        page = "<div class='form-group'>"
        page += "<select class='form-control' id='%s' name='%s'>" % (
            opts.get("name", ""),
            opts.get("name", ""),
        )
        for option in list:
            sel = ""
            if str(opts.get("value", "-1")) == str(option[0]):
                sel = "selected"
            page += "<option value='%s' %s>%s</option>" % (option[0], sel, option[1])
        page += "</select>"
        page += "</div>"
        return page

    def process_save_schedule(self):

        # Check that schedule dict exists within settings.
        # If not, this would indicate that this is the first time
        # we have saved the new schedule settings
        if (master.settings.get("Schedule", None) == None):
            master.settings["Schedule"] = {}

        # Slight issue with checkboxes, you have to default them all to
        # false, otherwise if one is unticked it is just not sent via form data
        days = [ "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday" ]
        for day in days:
            if (master.settings["Schedule"].get(day, None) == None):
                master.settings["Schedule"][day] = {}
            master.settings["Schedule"][day]["enabled"] = ""
            master.settings["Schedule"][day]["flex"] = ""

        # Detect schedule keys. Rather than saving them in a flat
        # structure, we'll store them multi-dimensionally
        fieldsout = self.fields.copy()
        ct = re.compile(r'(?P<trigger>enabled|end|flex|start)(?P<day>.*?)ChargeTime')
        for key in self.fields:
            match = ct.match(key)
            if match:
                # Detected a multi-dimensional (per-day) key
                # Rewrite it into the settings array and delete it
                # from the input

                if master.settings["Schedule"].get(match.group(2), None) == None:
                    # Create dictionary key for this day
                    master.settings["Schedule"][match.group(2)] = {}

                # Set per-day settings
                master.settings["Schedule"][match.group(2)][match.group(1)] = self.getFieldValue(key)

            else:
                if master.settings["Schedule"].get("Settings", None) == None:
                    master.settings["Schedule"]["Settings"] = {}
                master.settings["Schedule"]["Settings"][key] = self.getFieldValue(key)

        # During Phase 1 (backwards compatibility) for the new scheduling
        # UI, after writing the settings in the inteded new format, we then
        # write back to the existing settings nodes so that it is backwards
        # compatible.

        # Green Energy Tracking
        master.settings["hourResumeTrackGreenEnergy"] = int(master.settings["Schedule"]["Settings"]["resumeGreenEnergy"][:2])

        # Scheduled amps
        master.settings["scheduledAmpsStartHour"] = int(master.settings["Schedule"]["Common"]["start"][:2])
        master.settings["scheduledAmpsEndHour"] = int(master.settings["Schedule"]["Common"]["end"][:2])
        master.settings["scheduledAmpsMax"] = float(master.settings["Schedule"]["Settings"]["scheduledAmpsMax"])

        # Scheduled Days bitmap backward compatibility
        master.settings["scheduledAmpsDaysBitmap"] = (
            (1 if master.settings["Schedule"]["Monday"]["enabled"] else 0)
            + (2 if master.settings["Schedule"]["Tuesday"]["enabled"] else 0)
            + (4 if master.settings["Schedule"]["Wednesday"]["enabled"] else 0)
            + (8 if master.settings["Schedule"]["Thursday"]["enabled"] else 0)
            + (16 if master.settings["Schedule"]["Friday"]["enabled"] else 0)
            + (32 if master.settings["Schedule"]["Saturday"]["enabled"] else 0)
            + (64 if master.settings["Schedule"]["Sunday"]["enabled"] else 0)
            )

        # Save Settings
        master.queue_background_task({"cmd": "saveSettings"})

        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()
        self.wfile.write("".encode("utf-8"))
        return

    def process_save_settings(self):

        # This function will write the settings submitted from the settings
        # page to the settings dict, before triggering a write of the settings
        # to file
        for key in self.fields:

            # If the key relates to the car API tokens, we need to pass these
            # to the appropriate module, rather than directly updating the
            # configuration file (as it would just be overwritten)
            if (key == "carApiBearerToken" or key == "carApiRefreshToken") and self.getFieldValue(key) != "":
                carapi = master.getModuleByName("TeslaAPI")
                if key == "carApiBearerToken":
                    carapi.setCarApiBearerToken(self.getFieldValue(key))
                elif key == "carApiRefreshToken":
                    carapi.setCarApiRefreshToken(self.getFieldValue(key))

            # Write setting to dictionary
            master.settings[key] = self.getFieldValue(key)

        # If Non-Scheduled power action is either Do not Charge or
        # Track Green Energy, set Non-Scheduled power rate to 0
        if int(master.settings.get("nonScheduledAction", 1)) > 1:
            master.settings["nonScheduledAmpsMax"] = 0
        master.queue_background_task({"cmd": "saveSettings"})

        # Redirect to the index page
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()
        self.wfile.write("".encode("utf-8"))
        return

    def process_teslalogin(self):
        # Check if we are skipping Tesla Login submission

        if not master.teslaLoginAskLater:
            later = False
            try:
                later = len(self.fields["later"])
            except KeyError:
                later = False

            if later:
                master.teslaLoginAskLater = True

        if not master.teslaLoginAskLater:
            # Connect to Tesla API

            carapi = master.getModuleByName("TeslaAPI")
            carapi.setCarApiLastErrorTime(0)
            ret = carapi.car_api_available(
                self.fields["email"][0], self.fields["password"][0]
            )

            # Redirect to an index page with output based on the return state of
            # the function
            self.send_response(302)
            self.send_header("Location", "/teslaAccount/" + str(ret))
            self.end_headers()
            self.wfile.write("".encode("utf-8"))
            return
        else:
            # User has asked to skip Tesla Account submission for this session
            # Redirect back to /
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            self.wfile.write("".encode("utf-8"))
            return

    def show_twcs(self):

        page = """
        <table><tr width = '100%'><td width='65%'>
          <table class='table table-dark table-condensed table-striped'>
          <thead class='thead-dark'><tr>
            <th width='2%'>TWC ID</th>
            <th width='1%'>State</th>
            <th width='1%'>Version</th>
            <th width='2%'>Max Amps</th>
            <th width='2%'>Amps<br />Offered</th>
            <th width='2%'>Amps<br />In Use</th>
            <th width='2%'>Lifetime<br />kWh</th>
            <th width='4%'>Voltage<br />per Phase<br />1 / 2 / 3</th>
            <th width='2%'>Last Heartbeat</th>
            <th width='6%'>Vehicle Connected<br />Current / Last</th>
            <th width='2%'>Commands</th>
          </tr></thead>
        """
        for slaveTWC in master.getSlaveTWCs():
            twcid = "%02X%02X" % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
            page += "<tr>"
            page += "<td>%s</td>" % twcid
            page += "<td><div id='%s_state'></div></td>" % twcid
            page += "<td><div id='%s_version'></div></td>" % twcid
            page += "<td><div id='%s_maxAmps'></div></td>" % twcid
            page += "<td><div id='%s_lastAmpsOffered'></div></td>" % twcid
            page += "<td><div id='%s_reportedAmpsActual'></div></td>" % twcid
            page += "<td><div id='%s_lifetimekWh'></div></td>" % twcid
            page += (
                    "<td><span id='%s_voltsPhaseA'></span> / <span id='%s_voltsPhaseB'></span> / <span id='%s_voltsPhaseC'></span></td>"
                    % (twcid, twcid, twcid)
            )
            page += "<td><span id='%s_lastHeartbeat'></span> sec</td>" % twcid
            page += (
                    "<td>C: <span id='%s_currentVIN'></span><br />L: <span id='%s_lastVIN'></span></td>"
                    % (twcid, twcid)
            )
            page += """
            <td>
              <div class="dropdown">
                <button class="btn btn-secondary dropdown-toggle" type="button" id="dropdownMenuButton" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false">Select</button>
                <div class="dropdown-menu" aria-labelledby="dropdownMenuButton">
                  <a class="dropdown-item" href="#">Coming Soon</a>
                </div>
              </div>
            </td>
            """
            page += "</tr>"
        page += "<tr><td><b>Total</b><td>&nbsp;</td><td>&nbsp;</td>"
        page += "<td><div id='total_maxAmps'></div></td>"
        page += "<td><div id='total_lastAmpsOffered'></div></td>"
        page += "<td><div id='total_reportedAmpsActual'></div></td>"
        page += "<td><div id='total_lifetimekWh'></div></td>"
        page += "</tr></table></td></tr></table>"
        return page

    def debugLogAPI(self, message):
        master.debugLog(10, 
            "HTTPCtrl", 
            message
            + " (Url: "
            + str(self.url.path)
            + " / IP: "
            + str(self.client_address[0])
            + ")",
        )

  return HTTPControlHandler
