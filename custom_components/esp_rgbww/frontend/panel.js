const FLOW_API = "config/config_entries/options/flow";
const LABELS = {
  hardware: "Board and PWM",
  create_light: "Add light",
  create_light_pins: "Assign GPIO pins",
  delete_light: "Delete light",
  color: "Color configuration",
  network: "Network",
  firmware: "Firmware updates",
  icon: "Controller icon",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

class LightinatorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.controllers = [];
    this.selected = null;
    this.flow = null;
    this.values = {};
    this.busy = false;
    this.error = "";
    this.notice = "";
  }

  set hass(value) {
    this._hass = value;
    if (!this.started && this.isConnected) this.start();
  }

  connectedCallback() {
    if (this._hass && !this.started) this.start();
  }

  disconnectedCallback() {
    clearInterval(this.timer);
    this.started = false;
    // Options flows carry stale-edit snapshots; never retain an abandoned one.
    if (this.flow?.flow_id) {
      this._hass.callApi("DELETE", `${FLOW_API}/${this.flow.flow_id}`).catch(
        (error) => console.error("Unable to cancel Lightinator options flow", error),
      );
      this.flow = null;
    }
  }

  start() {
    this.started = true;
    this.refresh();
    this.timer = setInterval(() => {
      if (!this.busy && !this.flow) this.refresh();
    }, 30000);
  }

  async run(action) {
    if (this.busy) return;
    this.busy = true;
    this.error = "";
    this.render();
    try {
      await action();
    } catch (error) {
      this.error = error.message || String(error);
    } finally {
      this.busy = false;
      if (this.isConnected) this.render();
    }
  }

  async load() {
    if (this.translationLanguage !== this._hass.language) {
      const translations = await this._hass.callWS({
        type: "frontend/get_translations", language: this._hass.language,
        category: "options", integration: ["esp_rgbww"],
      });
      this.translations = translations.resources;
      this.translationLanguage = this._hass.language;
    }
    this.controllers = await this._hass.callWS({ type: "esp_rgbww/controllers" });
    if (this.selected) {
      const current = this.controllers.find((item) => item.entry_id === this.selected.entry_id);
      if (!current) {
        this.selected = null;
      } else {
        [this.selected] = await this._hass.callWS({
          type: "esp_rgbww/controllers", entry_id: current.entry_id,
        });
      }
    }
  }

  refresh() {
    return this.run(() => this.load());
  }

  select(controller) {
    return this.run(async () => {
      this.selected = controller;
      await this.load();
    });
  }

  text(key, fallback, placeholders = {}) {
    const fullKey = `component.esp_rgbww.options.${key}`;
    let text = this.translations?.[fullKey] || this._hass.localize(fullKey) || fallback;
    for (const [name, value] of Object.entries(placeholders || {})) {
      text = text.replaceAll(`{${name}}`, value);
    }
    return text;
  }

  async acceptFlow(flow, initial = {}) {
    this.flow = flow;
    this.values = {};
    if (flow.type === "create_entry") {
      this.flow = null;
      this.notice = "Settings saved. The controller may reconnect or restart.";
      await this.load();
    } else if (flow.type === "abort") {
      this.flow = null;
      throw new Error(this.text(`abort.${flow.reason}`, flow.reason, flow.description_placeholders));
    } else if (flow.type === "form") {
      for (const field of flow.data_schema) {
        if (field.default !== undefined) this.values[field.name] = field.default;
      }
      Object.assign(this.values, initial);
    } else if (flow.type !== "menu") {
      throw new Error(`Unsupported configuration step: ${flow.type}`);
    }
  }

  openSettings(step, lightId) {
    return this.run(async () => {
      this.notice = "";
      const menu = await this._hass.callApi("POST", FLOW_API, { handler: this.selected.entry_id });
      this.flow = menu;
      if (menu.type !== "menu") {
        await this.acceptFlow(menu);
        return;
      }
      const form = await this._hass.callApi("POST", `${FLOW_API}/${menu.flow_id}`, {
        next_step_id: step,
      });
      await this.acceptFlow(form, lightId ? { light_id: lightId } : {});
    });
  }

  submit() {
    return this.run(async () => {
      const previous = { ...this.values };
      const flow = await this._hass.callApi("POST", `${FLOW_API}/${this.flow.flow_id}`, previous);
      await this.acceptFlow(flow, flow.errors && Object.keys(flow.errors).length ? previous : {});
    });
  }

  cancel() {
    return this.run(async () => {
      if (this.flow?.flow_id) await this._hass.callApi("DELETE", `${FLOW_API}/${this.flow.flow_id}`);
      this.flow = null;
    });
  }

  button(label, action, secondary = false) {
    const button = node("button", label, secondary ? "secondary" : "");
    button.type = "button";
    button.disabled = this.busy;
    button.addEventListener("click", action);
    return button;
  }

  card(title, description) {
    const card = node("section", undefined, "card");
    card.append(node("h2", title));
    if (description) card.append(node("p", description, "muted"));
    return card;
  }

  renderField(field) {
    const wrapper = node("div", undefined, "field");
    const label = node("label", this.text(
      `step.${this.flow.step_id}.data.${field.name}`,
      field.name.replaceAll("_", " "),
    ) + (field.required ? " *" : ""));
    label.htmlFor = `field-${field.name}`;
    wrapper.append(label);
    const selector = field.selector || {};
    let input;
    const options = selector.select?.options || (field.type === "select" ? field.options : null);
    if (options) {
      input = node("select");
      const empty = node("option", "Select...");
      empty.value = "__unset";
      input.append(empty);
      for (const option of options) {
        const [value, label] = Array.isArray(option) ? option :
          typeof option === "object" ? [option.value, option.label] : [option, option];
        const item = node("option", label);
        item.value = value;
        input.append(item);
      }
      input.value = this.values[field.name] ?? "__unset";
      input.addEventListener("change", () => {
        if (input.selectedIndex === 0) delete this.values[field.name];
        else this.values[field.name] = input.value;
      });
    } else {
      input = node("input");
      const boolean = field.type === "boolean" || "boolean" in selector;
      const numeric = ["integer", "float"].includes(field.type) || "number" in selector;
      const supported = boolean || numeric || ["string", "text"].includes(field.type) || "text" in selector;
      if (!supported) throw new Error(`Unsupported form field: ${field.name} (${field.type})`);
      input.type = boolean ? "checkbox" : numeric ? "number" :
        field.name.endsWith("password") ? "password" : selector.text?.type || "text";
      if (boolean) {
        input.checked = this.values[field.name] ?? false;
        // HA's bool schema can have no default; still submit unchecked fields.
        this.values[field.name] = input.checked;
      } else {
        input.value = this.values[field.name] ?? "";
        if (field.required && !numeric && this.values[field.name] === undefined) {
          this.values[field.name] = "";
        }
      }
      if (numeric) {
        input.step = selector.number?.step ?? (field.type === "integer" ? "1" : "any");
        const min = selector.number?.min ?? field.valueMin;
        const max = selector.number?.max ?? field.valueMax;
        if (min !== undefined) input.min = min;
        if (max !== undefined) input.max = max;
      }
      input.addEventListener("input", () => {
        if (boolean) this.values[field.name] = input.checked;
        else if (input.value === "" && !(field.required && !numeric)) delete this.values[field.name];
        else this.values[field.name] = numeric ? Number(input.value) : input.value;
      });
    }
    input.disabled = this.busy;
    input.id = label.htmlFor;
    input.required = Boolean(field.required) &&
      (input.tagName === "SELECT" || input.type === "number" || (field.lengthMin || 0) > 0);
    if (field.lengthMin !== undefined) input.minLength = field.lengthMin;
    if (field.lengthMax !== undefined) input.maxLength = field.lengthMax;
    wrapper.append(input);
    const description = this.text(`step.${this.flow.step_id}.data_description.${field.name}`, "");
    if (description) wrapper.append(node("small", description, "muted"));
    const error = this.flow.errors?.[field.name];
    if (error) wrapper.append(node("small", this.text(`error.${error}`, error), "error"));
    return wrapper;
  }

  renderForm(main) {
    const step = this.flow.step_id;
    const card = this.card(this.text(`step.${step}.title`, LABELS[step] || step));
    const description = this.text(`step.${step}.description`, "", this.flow.description_placeholders);
    if (description) card.append(node("p", description, "muted"));
    if (this.flow.errors?.base) {
      card.append(node("p", this.text(`error.${this.flow.errors.base}`, this.flow.errors.base), "error"));
    }
    if (this.flow.type === "menu") {
      for (const option of this.flow.menu_options) {
        card.append(this.button(LABELS[option] || option, () => this.run(async () => {
          await this.acceptFlow(await this._hass.callApi("POST", `${FLOW_API}/${this.flow.flow_id}`, {
            next_step_id: option,
          }));
        })));
      }
    } else {
      const form = node("form");
      let previousGroup;
      for (const field of this.flow.data_schema) {
        const group = step === "network" ? (
          field.name.startsWith("api_") ? "API security" :
          field.name.startsWith("mqtt_") ? "MQTT" :
          field.name.startsWith("ha_") ? "MQTT discovery" :
          field.name.startsWith("rsyslog_") ? "Remote logging" :
          field.name.startsWith("telemetry_") ? "Telemetry" :
          field.name === "device_name" ? "Controller name" : "IP configuration"
        ) : step === "hardware" ? (
          field.name.startsWith("pwm_") ? "PWM timer" :
          field.name.startsWith("spread_") ? "Spread spectrum" :
          field.name.startsWith("phase_") ? "Phase shift" :
          field.name === "confirm" ? "Apply changes" : "Board"
        ) : null;
        if (group && group !== previousGroup) form.append(node("h3", group));
        previousGroup = group;
        form.append(this.renderField(field));
      }
      const save = node("button", "Continue");
      save.type = "submit";
      save.disabled = this.busy;
      form.append(save);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        this.submit();
      });
      card.append(form);
    }
    card.append(this.button("Cancel", () => this.cancel(), true));
    main.append(card);
  }

  render() {
    const style = node("style");
    style.textContent = `
      :host {display:block;color:var(--primary-text-color);background:var(--primary-background-color);
        min-height:100vh;font-family:var(--primary-font-family,system-ui);font-size:14px}
      * {box-sizing:border-box} header {display:flex;align-items:center;gap:12px;padding:12px 20px;
        background:var(--app-header-background-color,var(--primary-color));color:var(--app-header-text-color,#fff)}
      header h1 {font-size:22px;flex:1;margin:0} header button.secondary {color:inherit;border-color:currentColor}
      main {max-width:1100px;margin:auto;padding:24px} h2 {font-size:20px;margin:0 0 12px}
      h3 {margin:0 0 8px} p {line-height:1.5} .grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}
      .card {padding:22px;margin-bottom:18px;border:1px solid var(--divider-color);
        border-radius:16px;background:var(--card-background-color)} .muted,small {color:var(--secondary-text-color)}
      button {font:inherit;padding:10px 16px;border:1px solid transparent;border-radius:8px;
        background:var(--primary-color);color:var(--text-primary-color,#fff);cursor:pointer;margin:4px 8px 4px 0}
      button.secondary {background:transparent;color:var(--primary-color);border-color:var(--divider-color)}
      button:disabled {opacity:.5;cursor:wait} button:focus-visible,input:focus-visible,select:focus-visible {outline:2px solid var(--primary-color);outline-offset:3px}
      .field {display:flex;flex-direction:column;gap:8px;margin:18px 0} input,select {font:inherit;padding:12px;border-radius:8px;
        border:1px solid var(--divider-color);background:var(--primary-background-color);color:var(--primary-text-color);width:100%}
      input[type=checkbox] {width:24px;height:24px} .error {color:var(--error-color,#db4437)} .notice {padding:12px;border-left:4px solid var(--primary-color)}
      .badge {display:inline-block;font-size:12px;border:1px solid var(--divider-color);padding:3px 8px;border-radius:12px;margin:0 6px 6px 0}
      @media(max-width:600px) {main {padding:12px}.card {padding:16px}}
    `;
    const header = node("header");
    const menu = node("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this.narrow;
    header.append(menu, node("h1", "Lightinator"));
    if (!this.flow) header.append(this.button("Refresh", () => this.refresh(), true));
    const main = node("main");
    if (this.error) main.append(node("p", this.error, "error"));
    if (this.notice) main.append(node("p", this.notice, "notice"));
    try {
      if (this.flow) {
        this.renderForm(main);
      } else if (!this.selected) {
        main.append(node("h2", "Your controllers"));
        if (!this.controllers.length) main.append(node("p", this.busy ? "Loading..." : "No Lightinator controllers configured. Add one in Settings > Devices & services."));
        const grid = node("div", undefined, "grid");
        for (const controller of this.controllers) {
          const card = this.card(controller.name, controller.host);
          card.append(node("p", [controller.soc, controller.firmware].filter(Boolean).join(" / "), "muted"));
          card.append(node("span", controller.available ? "Connected" : controller.loaded ? "Unavailable" : "Not loaded", "badge"));
          card.append(this.button("Open controller", () => this.select(controller)));
          grid.append(card);
        }
        main.append(grid);
      } else {
        const controller = this.selected;
        main.append(this.button("All controllers", () => { this.selected = null; this.render(); }, true));
        const summary = this.card(controller.name, [controller.host, controller.soc, controller.firmware].filter(Boolean).join(" / "));
        summary.append(node("span", controller.available ? "Connected" : "Unavailable", "badge"));
        main.append(summary);
        if (!controller.loaded) {
          main.append(node("p", "Controller setup has not completed. Check its connection in Settings > Devices & services."));
        } else {
          const lights = this.card("Configured lights", controller.virtual ?
            "Lights belong to this controller. GPIOs are listed in channel order." : "Firmware 5.0 provides one light; virtual-light management requires 5.1.");
          if (controller.virtual) {
            lights.append(this.button("Add light", () => this.openSettings("create_light")));
            const grid = node("div", undefined, "grid");
            for (const light of controller.lights || []) {
              const card = this.card(light.name || light.id, `${light.type} / GPIO ${(light.pins || []).join(", ")}`);
              card.append(node("span", `Index ${light.index}`, "badge"));
              if (light.is_default) card.append(node("span", "Default", "badge"));
              card.append(this.button("Delete light", () => this.openSettings("delete_light", light.id), true));
              grid.append(card);
            }
            if (!controller.lights?.length) lights.append(node("p", "No lights configured. Add your first light above."));
            lights.append(grid);
          } else {
            lights.append(this.button("Color configuration", () => this.openSettings("color")));
          }
          main.append(lights);
          const settings = this.card("Controller settings", "These settings apply to the entire controller, not an individual light.");
          if (controller.board) settings.append(node("p", `Board: ${controller.board}`, "muted"));
          const timer = controller.pwm?.timer;
          if (timer) settings.append(node("p", `PWM: ${timer.frequency} Hz / ${timer.resolution}-bit / ${timer.speed_mode}`, "muted"));
          for (const step of [...(controller.virtual ? ["hardware"] : []), "network", "firmware", "icon"]) {
            settings.append(this.button(LABELS[step], () => this.openSettings(step), true));
          }
          main.append(settings);
        }
      }
    } catch (error) {
      main.append(node("p", error.message, "error"));
      if (this.flow) main.append(this.button("Cancel", () => this.cancel(), true));
    }
    this.shadowRoot.replaceChildren(style, header, main);
  }
}

customElements.define("lightinator-panel", LightinatorPanel);
