import { SearchSettings } from "./setup";
import {
  createDiv,
  createP,
  createSvg,
  makeIconPath,
  setBorderRadius,
  setDisplayFlex,
  setHeight6cqh,
} from "./utils";
import { placeholderColors } from "./lib/placeholderColors";
import { iconpaths } from "./lib/iconpaths";

export const createSearchbar = (
  iconFilename: string,
  settings: SearchSettings
): [
  HTMLElement,
  colorCb,
  (cb: (inputvalue: string) => void) => void,
  (fileName: string) => void
] => {
  const container = createDiv();
  container.id = settings.container.id;
  const setContainerStyle = () => {
    setDisplayFlex(container);
    container.style.placeItems = settings.container.style.placeItems;
    container.style.placeContent = settings.container.style.placeContent;
    setHeight6cqh(container);
  };
  const search = createP();
  search.textContent = settings.search.textContent;
  const setSearchStyle = () => {
    search.style.fontSize = settings.search.style.fontSize;
    search.style.marginRight = settings.search.style.marginRight;
  };
  const inputcontainer = createDiv();
  inputcontainer.id = settings.inputContainer.id;
  const setInputContainerStyle = () => {
    const style = inputcontainer.style;
    style.position = settings.inputContainer.style.position;
  };
  const setInputStyle = () => {
    const style = input.style;
    style.border = settings.input.style.border;
    style.padding = settings.input.style.padding;
    style.width = settings.input.style.width;
    setBorderRadius(settings.input.style.borderRadius)(input);
    style.backgroundColor = settings.input.style.bgColor;
    const { key, value } = settings.input.style.property;
    style.setProperty(key, value);
    style.maxHeight = settings.input.style.maxHeight;
  };
  setInputContainerStyle();
  const input = document.createElement("input");
  input.placeholder = settings.input.placeHolder;
  inputcontainer.appendChild(input);
  const searchIcon = createSvg(makeIconPath(iconFilename));
  searchIcon.id = settings.searchIcon.id;
  const inputValueCb = (onclick: (e: string) => void) => {
    searchIcon.onclick = (e: MouseEvent) => {
      onclick(input.value);
    };
  };
  const calculateTop = () => {
    const height = input.getBoundingClientRect().height;
    const iconHeight = searchIcon.getBoundingClientRect().height;
    const delta = height - iconHeight;
    if (height < iconHeight) {
      return delta / 2;
    } else if (height > iconHeight) {
      return delta / 2;
    } else {
      return 0;
    }
  };
  const setIconStyle = () => {
    const style = searchIcon.style;
    style.position = settings.searchIcon.style.position;
    style.right = settings.searchIcon.style.right;
    style.height = settings.searchIcon.style.height;
    style.cursor = settings.searchIcon.style.cursor;
  };
  const setTop = (top: number) => {
    const style = searchIcon.style;
    style.top = settings.searchIcon.style.top(top.toString());
  };
  const setIconSrc = (src: string) =>
    searchIcon.setAttribute("src", iconpaths.get(src));
  window.onload = () => {
    setIconStyle();
    const top = calculateTop();
    setTop(top);
  };
  window.onresize = () => {
    const top = calculateTop();
    setTop(top);
  };
  inputcontainer.appendChild(searchIcon);
  setContainerStyle();
  setSearchStyle();
  setInputStyle();
  container.appendChild(search);
  container.appendChild(inputcontainer);
  const setPlaceHolderColor = (color: string) =>
    input.style.setProperty(
      settings.input.style.property.key,
      placeholderColors.get(color)
    );
  return [container, setPlaceHolderColor, inputValueCb, setIconSrc];
};
