const themeToggle = document.querySelector('.theme-toggle');
const helloButton = document.querySelector('#hello-button');
const year = document.querySelector('#year');

year.textContent = new Date().getFullYear();

themeToggle.addEventListener('click', () => {
  const isDark = document.body.classList.toggle('dark');
  themeToggle.setAttribute('aria-label', `Switch to ${isDark ? 'light' : 'dark'} theme`);
  themeToggle.textContent = isDark ? '☾' : '☼';
});

helloButton.addEventListener('click', () => {
  helloButton.innerHTML = 'Hello there! <span>↗</span>';
});
