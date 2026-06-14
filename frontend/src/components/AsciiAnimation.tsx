"use client";

import { useEffect, useRef } from "react";

const CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%&*+-/=|<>[]{}~";
const ASCII_ART = [
  "  ███████  ██████  ██      ██    ██ ███    ██ ",
  " ██      ██      ██ ██      ██  ██  ████   ██ ",
  "  ███████ ██      ██ ██       ████   ██ ██  ██ ",
  "       ██ ██      ██ ██        ██    ██  ██ ██ ",
  " ███████   ██████  ███████     ██    ██   ████ ",
];

function randomChar() {
  return CHARS[Math.floor(Math.random() * CHARS.length)];
}

export function AsciiAnimation() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animationId = 0;
    let columns: number[] = [];
    let fontSize = 16;
    let frame = 0;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.parentElement?.getBoundingClientRect();
      const width = rect?.width ?? window.innerWidth;
      const height = rect?.height ?? window.innerHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      fontSize = Math.max(14, Math.min(20, Math.floor(width / 70)));
      ctx.font = `${fontSize}px "SFMono-Regular", "Cascadia Code", Consolas, monospace`;

      const colCount = Math.ceil(width / fontSize);
      columns = Array.from({ length: colCount }, () => Math.random() * -height);
    };

    resize();
    window.addEventListener("resize", resize);

    const drawAsciiArt = (width: number, height: number) => {
      const artHeight = ASCII_ART.length * fontSize;
      const artWidth = Math.max(...ASCII_ART.map((line) => line.length)) * (fontSize * 0.62);
      const startX = width - artWidth - fontSize * 2;
      const startY = height - artHeight - fontSize * 2;

      ASCII_ART.forEach((line, row) => {
        for (let i = 0; i < line.length; i++) {
          const char = line[i];
          if (char === " ") continue;
          const hue = (frame * 0.5 + i * 2 + row * 8) % 360;
          ctx.fillStyle = `hsla(${260 + hue * 0.12}, 42%, 58%, ${0.15 + Math.sin(frame * 0.05 + i) * 0.08})`;
          ctx.fillText(char, startX + i * fontSize * 0.62, startY + row * fontSize);
        }
      });
    };

    const draw = () => {
      const rect = canvas.parentElement?.getBoundingClientRect();
      const width = rect?.width ?? window.innerWidth;
      const height = rect?.height ?? window.innerHeight;

      ctx.clearRect(0, 0, width, height);

      for (let i = 0; i < columns.length; i++) {
        const x = i * fontSize;
        const y = columns[i];

        const isAccent = Math.random() > 0.88;
        ctx.fillStyle = isAccent
          ? `rgba(119, 84, 153, ${0.55 + Math.sin(frame * 0.08 + i) * 0.2})`
          : `rgba(104, 104, 121, ${0.32 + Math.cos(frame * 0.05 + i) * 0.1})`;
        ctx.fillText(randomChar(), x, y);

        if (y > height && Math.random() > 0.975) {
          columns[i] = 0;
        } else {
          columns[i] = y + fontSize;
        }
      }

      drawAsciiArt(width, height);
      frame++;
      animationId = requestAnimationFrame(draw);
    };

    draw();

    return () => {
      window.removeEventListener("resize", resize);
      cancelAnimationFrame(animationId);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className="ascii-canvas"
    />
  );
}
