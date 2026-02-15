const out = document.getElementById('output');

function render(data) {
  out.textContent = JSON.stringify(data, null, 2);
}

document.getElementById('createForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  payload.storageGB = Number(payload.storageGB);
  payload.ha = e.target.ha.checked;

  const res = await fetch('/api/mysql', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });

  const data = await res.json();
  render({ status: res.status, body: data });
});

document.getElementById('statusForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const namespace = fd.get('namespace');
  const name = fd.get('name');

  const res = await fetch(`/api/status/mysql/${namespace}/${name}`);
  const data = await res.json();
  render({ status: res.status, body: data });
});
