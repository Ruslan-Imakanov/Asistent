# Публичный сайт «Нейро Офиса» (Cloudflare Worker)

- `worker.js` — Worker `neuro-office`: отдаёт `site/<имя>.html` по адресу `/<имя>` (`index.html` — главная). Новая страница появляется на сайте через ~5 минут после пуша.
- `salon.html` — публичная версия демо-лендинга (фото подгружаются с Unsplash).
- `salon_template.html` — исходник страницы с метками `{{nails}}` и т.п. для фото.

Адрес: https://neuro-office.imakanovr.workers.dev
