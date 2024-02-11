import { bigSection, medSection, spaces } from "./setup";
import { tabColorsMap } from "./lib/tabColorsMap";

export const openPopUp = (url: string, target: string, features: string) =>
  window.open(url, target, features);

export const setMargin = (margin: string) => (element: HTMLElement) =>
  (element.style.margin = margin);
export const setMarginBottom = (margin: string) => (element: HTMLElement) =>
  (element.style.marginBottom = margin);
export const setHeight = (height: string) => (element: HTMLElement) =>
  (element.style.height = height);
export const setDisplay = (display: string) => (element: HTMLElement) =>
  (element.style.display = display);
export const setBorderRadius = (radius: string) => (element: HTMLElement) =>
  (element.style.borderRadius = radius);

// utils
export const setMargin0 = setMargin("0");
export const setMargin1rem = setMargin("1rem");
export const setHeight6cqh = setHeight(`${medSection}cqh`);
export const setHeight9cqh = setHeight(`${bigSection}cqh`);
export const setHeight91cqh = setHeight(
  `${100 - spaces.reduce((p, c) => p + c)}cqh`
);
export const setDisplayFlex = setDisplay("flex");
export const setDisplayGrid = setDisplay("grid");
export const setElementStyle = (element: HTMLElement, text: HTMLElement) => {
  setMargin("3px")(element);
  setBorderRadius("4px")(element);
  text.style.padding = "0.3rem";
  text.style.color = "black";
};
export const setElementColor = (element: HTMLElement) => (color: string) => {
  element.style.color = tabColorsMap.get(color);
};

export const setElementBgColor = (element: HTMLElement) => (color: string) => {
  element.style.backgroundColor = tabColorsMap.get(color);
};

export const createDiv = () => document.createElement("div");
export const createSvg = (src: string, height: string = "1rem") => {
  const svg = document.createElement("img");
  svg.setAttribute("src", src);
  setHeight(height)(svg);
  return svg;
};
export const createP = () => {
  const p = document.createElement("p");
  setMargin0(p);
  return p;
};
export const createPSetText = (text: string, marginBottom: string = "1rem") => {
  const p = createP();
  p.textContent = text;

  setMarginBottom(marginBottom)(p);
  return p;
};
export const createButton = (image: string) => {
  const button = document.createElement("button");
  button.style.border = "none";
  button.style.backgroundColor = "white";
  button.style.borderRadius = "4px";
  const svg = createSvg(image);
  svg.style.height = "1.3rem";
  button.appendChild(svg);
  return button;
};

export const makeIconPath = (icon: string) => `../Assets/icons/${icon}.svg`;

export const makeLinearGradient = (colora: string, colorb: string) =>
  `linear-gradient(180deg,${colora},${colorb})`;
