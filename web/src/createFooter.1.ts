import { FooterSettings } from "./setup";
import { createDiv, createSvg, setDisplayFlex, setDisplayGrid } from "./utils";
import { gradientMap } from "./lib/gradientMap";

export const createFooter = (
  icons: [string, string][],
  settings: FooterSettings
): [HTMLElement, colorCb] => {
  const container = createDiv();
  container.id = settings.container.id;
  const subcontainer = createDiv();
  subcontainer.id = settings.container.subcontainer.id;
  container.appendChild(subcontainer);
  setDisplayFlex(container);
  setDisplayGrid(subcontainer);
  const setContainerStyle = () => {
    const style = container.style;
    style.height = settings.container.style.height;
    style.placeContent = settings.container.style.placeContent;
    style.placeItems = settings.container.style.placeItems;
  };
  const setSubContainerStyle = () => {
    const style = subcontainer.style;
    style.width = settings.container.subcontainer.style.width;
    style.gridTemplateColumns =
      settings.container.subcontainer.style.gridTemplateColumns;
    style.placeSelf = settings.container.subcontainer.style.placeSelf;
    style.placeItems = settings.container.subcontainer.style.placeItems;
  };
  setContainerStyle();
  setSubContainerStyle();
  const setColor = (color: string) =>
    (container.style.backgroundImage = gradientMap.get(color)[1]);
  const elements = icons.map((e) => {
    const a = document.createElement("a");
    const svg = createSvg(e[0], settings.icons.style.height);
    a.appendChild(svg);
    a.style.cursor = settings.icons.style.cursor;
    a.setAttribute(settings.icons.style.attribute1.key, e[1]);
    a.setAttribute(
      settings.icons.style.attribute2.key,
      settings.icons.style.attribute2.value
    );
    return a;
  });
  elements.forEach((e, i, a) => {
    subcontainer.appendChild(e);
  });
  return [container, setColor];
};
