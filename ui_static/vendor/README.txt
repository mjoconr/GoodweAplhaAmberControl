Place these files here to make the React UI work fully offline (no CDN):

  - react.production.min.js
  - react-dom.production.min.js

The UI server will serve them at:
  /vendor/react.production.min.js
  /vendor/react-dom.production.min.js

If they are missing, ui_server.py will fall back to loading React from a public CDN.
The Classic UI at /classic never needs any external JS.
