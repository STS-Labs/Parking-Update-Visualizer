// Approximate names for common sign codes (Georgian / Vienna-convention numbering).
// Unknown codes are shown as the bare code. Extend freely.
window.SIGN_NAMES = {
  "1.23": "Children",
  "2.1": "Priority road",
  "2.2": "End of priority road",
  "2.3": "Intersection with secondary road",
  "2.4": "Give way",
  "2.5": "Stop",
  "3.1": "No entry",
  "3.2": "No vehicles",
  "3.18.1": "No right turn",
  "3.18.2": "No left turn",
  "3.19": "No U-turn",
  "3.20": "No overtaking",
  "3.24": "Speed limit",
  "3.27": "No stopping",
  "3.28": "No parking",
  "3.29": "No parking on odd days",
  "3.30": "No parking on even days",
  "4.1.1": "Ahead only",
  "4.1.2": "Turn right only",
  "4.1.3": "Turn left only",
  "4.1.4": "Ahead or right only",
  "4.1.5": "Ahead or left only",
  "4.2.1": "Keep right",
  "4.2.2": "Keep left",
  "5.5": "One-way road",
  "5.15.1": "Lane directions",
  "5.15.2": "Lane direction",
  "5.16": "Bus stop",
  "5.19.1": "Pedestrian crossing",
  "5.19.2": "Pedestrian crossing",
  "6.4": "Parking",
  "8.2.1": "Plate: zone length",
  "8.5.4": "Plate: time of operation"
};

window.signName = function (code) {
  const m = /^(.+?)_(\d+)$/.exec(code); // e.g. "3.24_40" -> speed limit 40
  const base = m ? m[1] : code;
  const name = window.SIGN_NAMES[base];
  if (!name) return "";
  return m ? `${name} ${m[2]}` : name;
};
