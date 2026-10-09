// Short messages shown at the top of the screen (e.g. the clock reached the end of the data).

const el = () => document.getElementById('notice');
let timer = null;

export function notify(message, ms = 7000) {
  el().textContent = message;
  el().classList.add('show');
  clearTimeout(timer);
  timer = setTimeout(() => el().classList.remove('show'), ms);
}
