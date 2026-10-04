// Runs the real picker script against a changing DOM, without a browser.
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let opened = false, view = 'simple', position = 1, selected = 'Other';
const events = []; let focused;
class Element {
  constructor(text = '', attrs = {}, children = () => []) {
    this.textContent = text; this.innerText = text; this.attrs = attrs; this.children = children;
  }
  getClientRects() { return [1]; }
  closest() { return null; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  setAttribute(k,v) { this.attrs[k] = v; }
  querySelector(s) { return this.querySelectorAll(s)[0] ?? null; }
  querySelectorAll(s) { return this.children(s); }
  focus() { focused = this; events.push('focus'); }
  dispatchEvent(e) { events.push(e.key); return true; }
}
const names = ['Instant', 'Medium', 'High', 'Extra High', 'Pro'];
const picker = new Element('Medium');
const hiddenPicker = new Element('Pro');
hiddenPicker.getClientRects = () => [];
picker.dispatchEvent = e => { if (e.type === 'keydown' && e.key === 'ArrowDown') opened = true; };
const toggle = new Element(); toggle.click = () => { view = 'models'; };
const slider = new Element('', {'aria-valuemin': '0', 'aria-valuemax': input.workSlider ? '5' : '4', 'aria-hidden': 'true'});
slider.getAttribute = k => k === 'aria-valuenow' ? String(position) : slider.attrs[k] ?? null;
const status = new Element();
Object.defineProperty(status, 'textContent', {get: () => input.badStatus ? 'Unknown, 2 of 5.' : `${names[position]}, ${position + 1} of 5.`});
const power = new Element('', {}, s => s === '[role="slider"]' ? [slider] : []);
power.dispatchEvent = e => {
  events.push(e.key);
  if (e.type === 'keydown') position += e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
};
const rows = ['Latest', 'Other'].map(name => {
  const row = new Element(name + ' Leaving on October 14', {}, s => s === 'span' ? [new Element(name), new Element('Leaving on October 14')] : []);
  row.getAttribute = k => k === 'aria-checked' ? String(selected === name) : null;
  row.click = () => { selected = name; opened = false; view = 'simple'; };
  return row;
});
const menu = new Element('', {}, s => {
  if (s === '[role="menuitemradio"]') return input.noRows ? [] : rows;
  if (s === '[role="slider"]') return [slider];
  if (s === '[data-model-picker-view-toggle="true"]') return view === 'simple' ? [toggle] : [];
  if (s === '[data-reasoning-slider="true"]') return view === 'simple' ? [power] : [];
  if (s === '[role="status"]') return view === 'simple' ? [status] : [];
  return [];
});
menu.dispatchEvent = e => { if (e.type === 'keydown' && e.key === 'Escape') { opened = false; view = 'simple'; } };
global.document = {
 querySelectorAll: s => s === '[role="menu"]' ? opened ? [menu] : [] : [hiddenPicker, picker],
 querySelector: s => s.includes('data-crossfeed-wake') ? rows.find(r => r.attrs['data-crossfeed-wake'] === 'row') : s.includes('data-model-picker-view-toggle') ? toggle : s.includes('data-reasoning-slider') ? power : hiddenPicker
};
global.press = key => {
 if (key === 'Escape') menu.dispatchEvent({type:'keydown',key});
 else focused.dispatchEvent({type:'keydown',key});
};
global.KeyboardEvent = class {constructor(type, options) { this.type = type; Object.assign(this, options); }};
(async () => {
  const value = await eval(input.script);
  console.log(JSON.stringify({value, selected, position, opened, events}));
})().catch(e => { console.error(e.message); process.exitCode = 1; });
