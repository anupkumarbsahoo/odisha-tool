"""Records one manual run of the site in Firefox (same profile as the tool).

Writes to recording/:
  recording.txt  - one readable line per action (click / type / select / upload)
  steps.jsonl    - the same actions with full element details (id, name, label, options)
  page_NN.html   - HTML snapshot taken at each page load and each action, for reading real selectors
Close the Firefox window to finish.
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "recording"

LISTENER_JS = r"""
(() => {
  if (window.__recorderInstalled) return;
  window.__recorderInstalled = true;
  // The page sends its own HTML so Python never calls back into the browser mid-navigation.
  const send = step => {
    step.html = document.documentElement.outerHTML;
    window.__recordStep(step);
  };
  const pageLoaded = () => send({action: 'page', url: location.href, el: {}});
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', pageLoaded);
  else pageLoaded();
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.innerText.trim();
    }
    const wrap = el.closest('label') || el.closest('div');
    const l = wrap && wrap.querySelector('label');
    return l ? l.innerText.trim() : '';
  };
  const describe = el => ({
    tag: el.tagName.toLowerCase(),
    type: el.type || '',
    id: el.id || '',
    name: el.name || '',
    cls: (el.className && el.className.baseVal === undefined) ? el.className : '',
    label: labelFor(el),
    text: (el.innerText || el.value || '').trim().slice(0, 80),
    placeholder: el.placeholder || '',
    onclick: el.getAttribute('onclick') || '',
    href: el.getAttribute('href') || '',
  });
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input[type=radio],input[type=checkbox],input[type=submit],input[type=button],label,[onclick]') || e.target;
    if (el.tagName === 'SELECT' || el.tagName === 'OPTION') return;
    send({action: 'click', url: location.href, el: describe(el)});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    const d = describe(el);
    if (el.tagName === 'SELECT') {
      const opt = el.options[el.selectedIndex];
      send({action: 'select', url: location.href, el: d,
        value: opt ? opt.value : '', option_text: opt ? opt.text.trim() : '',
        options: Array.from(el.options).map(o => o.text.trim()).slice(0, 300)});
    } else if (el.type === 'file') {
      send({action: 'upload', url: location.href, el: d,
        files: Array.from(el.files).map(f => f.name)});
    } else if (el.type !== 'radio' && el.type !== 'checkbox') {
      send({action: 'type', url: location.href, el: d, value: el.value});
    }
  }, true);
})();
"""


def selector_hint(el):
    if el.get("id"):
        return f"#{el['id']}"
    if el.get("name"):
        return f"{el['tag']}[name=\"{el['name']}\"]"
    if el.get("text") and el["tag"] in ("a", "button"):
        role = "link" if el["tag"] == "a" else "button"
        return f"role={role}[name=/{el['text']}/i]"
    return f"{el['tag']} (label: {el.get('label', '')})"


def main():
    config = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(exist_ok=True)
    steps_file = (OUT_DIR / "steps.jsonl").open("w", encoding="utf-8")
    text_file = (OUT_DIR / "recording.txt").open("w", encoding="utf-8")
    state = {"n": 0}

    def record_step(step):
        step["time"] = time.strftime("%H:%M:%S")
        html = step.pop("html", None)
        if html:
            state["n"] += 1
            step["snapshot"] = f"page_{state['n']:02d}.html"
            (OUT_DIR / step["snapshot"]).write_text(html, encoding="utf-8")
        steps_file.write(json.dumps(step, ensure_ascii=False) + "\n")
        steps_file.flush()
        if step["action"] == "page":
            line = f"{step['time']}  page    {step['url']}"
        else:
            el = step["el"]
            detail = {"click": el.get("text", ""),
                      "type": step.get("value", ""),
                      "select": step.get("option_text", ""),
                      "upload": ", ".join(step.get("files", []))}[step["action"]]
            line = f"{step['time']}  {step['action']:<6}  {selector_hint(el):<60}  label='{el.get('label', '')}'  -> {detail}"
        text_file.write(line + "\n")
        text_file.flush()
        print(line)

    profile = BASE_DIR / config.get("browser_profile_dir", "profile/firefox")
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.firefox.launch_persistent_context(str(profile), headless=False, no_viewport=True)
        context.expose_function("__recordStep", record_step)
        context.add_init_script(LISTENER_JS)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(config["login_url"], wait_until="domcontentloaded")
        print("Recording. Do the full flow in Firefox, then close the window.")
        try:
            context.wait_for_event("close", timeout=0)
        except Exception:
            pass
    steps_file.close()
    text_file.close()
    print(f"Saved to {OUT_DIR}")


if __name__ == "__main__":
    main()
