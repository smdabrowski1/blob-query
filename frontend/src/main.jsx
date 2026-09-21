import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";

// Self-hosted (no runtime call to Google Fonts — same "don't depend on a
// third party for the app to render" reasoning as the rest of the frontend).
// Weight subset only: 400/500 for body/labels, 600/700 for emphasis/headings.
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";

import "./styles.css";

createRoot(document.getElementById("root")).render(<App />);
