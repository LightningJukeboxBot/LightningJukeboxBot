// web/src/setup.ts
var darkRed = `#c50200`;
var superDark = `#720000`;
var medSection = 6;
var bigSection = 9;
var spaces = [medSection, medSection, bigSection];

// web/src/utils.ts
var setMargin = (margin) => (element) => element.style.margin = margin;
var setMarginBottom = (margin) => (element) => element.style.marginBottom = margin;
var setHeight = (height) => (element) => element.style.height = height;
var setDisplay = (display) => (element) => element.style.display = display;
var setMargin0 = setMargin("0");
var setMargin1rem = setMargin("1rem");
var setHeight6cqh = setHeight(`${medSection}cqh`);
var setHeight9cqh = setHeight(`${bigSection}cqh`);
var setHeight91cqh = setHeight(`${100 - spaces.reduce((p, c) => p + c)}cqh`);
var setDisplayFlex = setDisplay("flex");
var setDisplayGrid = setDisplay("grid");
var createDiv = () => document.createElement("div");
var createSvg = (src, height = "1rem") => {
  const svg = document.createElement("img");
  svg.setAttribute("src", src);
  setHeight(height)(svg);
  return svg;
};
var createP = () => {
  const p = document.createElement("p");
  setMargin0(p);
  return p;
};
var createPSetText = (text, marginBottom = "1rem") => {
  const p = createP();
  p.textContent = text;
  setMarginBottom(marginBottom)(p);
  return p;
};
var makeIconPath = (icon) => `../Assets/icons/${icon}.svg`;
var makeLinearGradient = (colora, colorb) => `linear-gradient(180deg,${colora},${colorb})`;

// web/src/popup.ts
var MARGINBOTTOMSM = "0.2rem";
var body = document.body;
var createPopUp = () => {
  const container = createDiv();
  const setContainerStyle = () => {
    const style = container.style;
    style.backgroundImage = makeLinearGradient(darkRed, superDark);
    style.height = "100%";
    style.fontFamily = "boucherie-block";
    style.color = "white";
    style.display = "flex";
    style.flexDirection = "column";
    style.alignContent = "center";
    style.textAlign = "center";
    style.padding = "0.8rem";
  };
  setContainerStyle();
  const createTitle = () => {
    return createPSetText("INFO");
  };
  const createSearchIcon = () => {
    const searchicon = createSvg(makeIconPath("red-search"), "24px");
    setMarginBottom(MARGINBOTTOMSM)(searchicon);
    return searchicon;
  };
  const createFirstInfo = () => createPSetText("USE THE SEARCH FUNCTION AND SELECT THE BEST RESULT");
  const createUpDownIconst = () => {
    const container2 = createDiv();
    const setContainerStyle2 = () => {
      setDisplayFlex(container2);
      const style = container2.style;
      style.placeContent = "center";
    };
    setContainerStyle2();
    const downpath = makeIconPath("downvote-arrow");
    const uppath = makeIconPath("upvote-arrow");
    [downpath, uppath].map((e, i) => {
      const container3 = createDiv();
      const svg = createSvg(e);
      const setSvgStyle = () => {
        const style = svg.style;
        if (i == 0)
          style.marginRight = MARGINBOTTOMSM;
        style.padding = "0.2rem 0.4rem";
        style.backgroundColor = "white";
        style.borderRadius = "5px";
      };
      setSvgStyle();
      container3.appendChild(svg);
      return container3;
    }).forEach((e) => container2.appendChild(e));
    setMarginBottom(MARGINBOTTOMSM)(container2);
    return container2;
  };
  const createSecondInfo = () => createPSetText(`YOU MAY CHOOSE TO UP- OR DOWNVOTE TRACKS IN THE QUEUE`);
  const createInfoIcon = () => {
    const svg = createSvg(makeIconPath("info"), "24px");
    setMarginBottom(MARGINBOTTOMSM)(svg);
    return svg;
  };
  const createThirdInfo = () => {
    const container2 = createDiv();
    const firstLine = createPSetText(`THERE ARE THREE CHANNELS`, MARGINBOTTOMSM);
    const secondLine = createPSetText(`RED, GREEN AND BLUE`, MARGINBOTTOMSM);
    const thirdLine = createPSetText(`EACH CHANNEL HAS ITS OWN QUEUE PRICE`);
    [firstLine, secondLine, thirdLine].forEach((e) => container2.appendChild(e));
    return container2;
  };
  const createHaveFun = () => createPSetText("HAVE FUN!");
  container.appendChild(createTitle());
  container.appendChild(createSearchIcon());
  container.appendChild(createFirstInfo());
  container.appendChild(createUpDownIconst());
  container.appendChild(createSecondInfo());
  container.appendChild(createInfoIcon());
  container.appendChild(createThirdInfo());
  container.appendChild(createHaveFun());
  return container;
};
var popup = createPopUp();
body.prepend(popup);
