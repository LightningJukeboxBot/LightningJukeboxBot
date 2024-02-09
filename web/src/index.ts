console.log("test");
export {};

const createUI = () => {
  type eventCb = (cb: (e: MouseEvent) => void) => void;
  type colorCb = (color: string) => void;

  const vote = true;

  const innerRed = `#ff8a8d`;
  const darkRed = `#c50200`;
  const superDark = `#720000`;
  const greenLight = `#a5f782`;
  const greencolor = `#73d04c`;
  const greenDark = `#27821a`;
  const blueLight = `#8ba5f6`;
  const bluecolor = `#5e84fc`;
  const blueDark = `#0d459d`;
  const fontMid = `"1.3rem"`;
  const tabwidth = 6;
  const tabHeight = 7;
  const footerHeight = 7;
  const gridTemplate = "1fr 5fr 1fr";
  const tabIconsSize = 32;
  const medSection = 6;
  const bigSection = 9;
  const spaces = [medSection, medSection, bigSection];

  const getResults = () => {
    return new Array(25).fill("EMPTY");
  };

  const getQueue = () => {
    return new Array(25).fill("I WILL BE THERE");
  };

  const map = new Map();
  map.set("red-tab", innerRed);
  map.set("green-tab", greenLight);
  map.set("blue-tab", blueLight);
  const setElementColor = (element: HTMLElement) => (color: string) => {
    element.style.color = map.get(color);
  };
  const setElementBgColor = (element: HTMLElement) => (color: string) => {
    element.style.backgroundColor = map.get(color);
  };

  const makeLinearGradient = (colora: string, colorb: string) =>
    `linear-gradient(180deg,${colora},${colorb})`;

  const makeIconPath = (icon: string) => `../Assets/icons/${icon}.svg`;

  const redGradient = makeLinearGradient(darkRed, superDark);
  const alldarkred = makeLinearGradient(superDark, superDark);
  const greenGradient = makeLinearGradient(greencolor, greenDark);
  const alldarkgreen = makeLinearGradient(greenDark, greenDark);
  const blueGradient = makeLinearGradient(bluecolor, blueDark);
  const alldarkblue = makeLinearGradient(blueDark, blueDark);

  const gradientMap = new Map();
  gradientMap.set("red-tab", [redGradient, alldarkred]);
  gradientMap.set("red-tab-icon", [redGradient, alldarkred]);
  gradientMap.set("green-tab", [greenGradient, alldarkgreen]);
  gradientMap.set("green-tab-icon", [greenGradient, alldarkgreen]);
  gradientMap.set("blue-tab", [blueGradient, alldarkblue]);
  gradientMap.set("blue-tab-icon", [blueGradient, alldarkblue]);
  const iconpaths = new Map();
  iconpaths.set("red-tab", makeIconPath("red-search"));
  iconpaths.set("red-tab-icon", makeIconPath("red-search"));
  iconpaths.set("green-tab", makeIconPath("green-search"));
  iconpaths.set("green-tab-icon", makeIconPath("green-search"));
  iconpaths.set("blue-tab", makeIconPath("blue-search"));
  iconpaths.set("blue-tab-icon", makeIconPath("blue-search"));
  const placeholderColors = new Map();
  placeholderColors.set("red-tab", darkRed);
  placeholderColors.set("red-tab-icon", darkRed);
  placeholderColors.set("green-tab", greencolor);
  placeholderColors.set("green-tab-icon", greencolor);
  placeholderColors.set("blue-tab", bluecolor);
  placeholderColors.set("blue-tab-icon", bluecolor);

  const setMargin = (margin: string) => (element: HTMLElement) =>
    (element.style.margin = margin);
  const setHeight = (height: string) => (element: HTMLElement) =>
    (element.style.height = height);
  const setDisplay = (display: string) => (element: HTMLElement) =>
    (element.style.display = display);
  const setBorderRadius = (radius: string) => (element: HTMLElement) =>
    (element.style.borderRadius = radius);
  // utils
  const setMargin0 = setMargin("0");
  const setMargin1rem = setMargin("1rem");
  const setHeight6cqh = setHeight(`${medSection}cqh`);
  const setHeight9cqh = setHeight(`${bigSection}cqh`);
  const setHeight91cqh = setHeight(
    `${100 - spaces.reduce((p, c) => p + c)}cqh`
  );
  const setDisplayFlex = setDisplay("flex");
  const setDisplayGrid = setDisplay("grid");
  const setElementStyle = (element: HTMLElement, text: HTMLElement) => {
    setMargin("3px")(element);
    setBorderRadius("4px")(element);
    text.style.padding = "0.3rem";
    text.style.color = "black";
  };

  const createDiv = () => document.createElement("div");
  const createSvg = (src: string, height: string = "1rem") => {
    const svg = document.createElement("img");
    svg.setAttribute("src", src);
    setHeight(height)(svg);
    return svg;
  };
  const createP = () => {
    const p = document.createElement("p");
    setMargin0(p);
    return p;
  };
  const createButton = (image: string) => {
    const button = document.createElement("button");
    button.style.border = "none";
    button.style.backgroundColor = "white";
    button.style.borderRadius = "4px";
    const svg = createSvg(image);
    svg.style.height = "1.3rem";
    button.appendChild(svg);
    return button;
  };
  //// UI
  const createWebui = () => {
    const container = createDiv();
    container.id = "webui";
    const setWebUIStyle = () => {
      const style = container.style;
      style.height = "100vh";
      style.containerType = "size ";
      style.fontFamily = "boucherie-block";
      style.color = "white";
    };
    setWebUIStyle();
    return container;
  };
  const createTabs = (): [HTMLElement, [eventCb, eventCb, eventCb]] => {
    const container = createDiv();
    container.id = "tabs-area";
    container.style.position = "relative";
    setDisplayFlex(container);
    container.style.height = tabHeight + "cqh";
    // RED
    const red = createDiv();
    red.id = "red-tab";
    const redsvg = createSvg(makeIconPath("Red-tab-icon"), `${tabIconsSize}px`);
    redsvg.id = "red-tab-icon";
    red.appendChild(redsvg);
    red.style.backgroundColor = darkRed;
    // GREEN
    const green = createDiv();
    green.id = "green-tab";
    const greenp = createP();
    const greensvg = createSvg(
      makeIconPath("green-tab-icon"),
      `${tabIconsSize}px`
    );
    green.appendChild(greensvg);
    greensvg.id = "green-tab-icon";
    const gr = greencolor;
    greenp.textContent = gr;
    green.style.backgroundColor = gr;
    green.style.left = tabwidth + `rem`;
    // BLUE
    const blue = createDiv();
    blue.id = "blue-tab";
    const bluesvg = createSvg(
      makeIconPath("blue-tab-icon"),
      `${tabIconsSize}px`
    );
    bluesvg.id = "blue-tab-icon";
    blue.appendChild(bluesvg);
    blue.style.backgroundColor = bluecolor;
    blue.style.left = tabwidth * 2 + `rem`;
    setHeight("20px")(red);
    setHeight("20px")(green);
    setHeight("20px")(blue);
    const setTabStyle = (tab: HTMLElement) => {
      const style = tab.style;
      style.width = tabwidth + `rem`;
      style.position = "absolute";
      style.bottom = "0";
      style.height = "-webkit-fill-available";
      style.borderRadius = "5px 5px 0 0";
      style.display = "grid";
      style.placeItems = "center";
    };
    const setRedCb = (cb: (e: MouseEvent) => void) => {
      red.onclick = cb;
    };
    const setGreebCb = (cb: (e: MouseEvent) => void) => {
      green.onclick = cb;
    };
    const setBlueCb = (cb: (e: MouseEvent) => void) => {
      blue.onclick = cb;
    };
    [red, green, blue].forEach((e) => setTabStyle(e));
    container.appendChild(red);
    container.appendChild(green);
    container.appendChild(blue);
    return [container, [setRedCb, setGreebCb, setBlueCb]];
  };
  const createDisplay = (): [HTMLElement, colorCb] => {
    const container = createDiv();
    container.id = "display-area";
    container.style.height =
      100 - [tabHeight, footerHeight].reduce((p, c) => p + c) + "cqh";
    container.style.containerType = "size";
    const setColor = (color: string) =>
      (container.style.backgroundImage = gradientMap.get(color));

    return [container, setColor];
  };
  const createHeader = (songinfostext: string) => {
    const container = createDiv();
    container.style.placeItems = "center";
    container.style.placeContent = "space-evenly";
    container.id = "jb-header";
    setHeight9cqh(container);
    const nowPlaying = createP();
    nowPlaying.textContent = "NOW PLAYING: " + songinfostext;
    const setNowPlayingStyle = () => {
      const style = nowPlaying.style;
      const margintb = "0.3rem";
      const marginlr = "0.2rem";
      style.paddingTop = margintb;
      style.paddingRight = marginlr;
      style.paddingBottom = margintb;
      style.paddingLeft = marginlr;
      style.borderTop = "solid 1px white";
      style.borderBottom = "solid 1px white";
      style.fontSize = fontMid;
    };
    setNowPlayingStyle();
    setDisplayFlex(container);
    container.appendChild(nowPlaying);
    return container;
  };
  const createInfos = (text: string): [HTMLElement, eventCb, colorCb] => {
    const container = createDiv();
    container.id = "jukebox-infos";
    container.style.placeItems = "center";
    container.style.placeContent = "center";
    setHeight6cqh(container);
    setDisplayFlex(container);
    const infosvg = createSvg(makeIconPath("info"));
    const infos = createP();
    infosvg.style.cursor = "pointer";
    const setOnClick = (cb: (e: MouseEvent) => void) => (infosvg.onclick = cb);
    infos.textContent = text;
    infos.style.fontSize = "0.8rem";
    infos.style.marginLeft = "0.5rem";
    infos.style.color = innerRed;
    const setInfosTextColor = setElementColor(infos);
    container.appendChild(infosvg);
    container.appendChild(infos);
    return [container, setOnClick, setInfosTextColor];
  };
  const createSearchbar = (
    iconFilename: string
  ): [
    HTMLElement,
    colorCb,
    (cb: (inputvalue: string) => void) => void,
    (fileName: string) => void
  ] => {
    const container = createDiv();
    container.id = "search-bar";
    container.style.placeItems = "center";
    container.style.placeContent = "space-evenly";
    setHeight6cqh(container);
    const search = createP();
    setDisplayFlex(container);
    search.textContent = "SEARCH";
    search.style.fontSize = fontMid;
    const inputcontainer = createDiv();
    inputcontainer.id = "search-input";
    const setInputContainerStyle = () => {
      const style = inputcontainer.style;
      style.position = "relative";
    };
    setInputContainerStyle();
    const input = document.createElement("input");
    input.placeholder = "ARTIST TRACK + TITLE";
    inputcontainer.appendChild(input);
    const searchIcon = createSvg(makeIconPath(iconFilename));
    searchIcon.id = "search-icon";
    const inputValueCb = (onclick: (e: string) => void) => {
      searchIcon.onclick = (e: MouseEvent) => {
        onclick(input.value);
      };
    };
    const setIconStyle = () => {
      const style = searchIcon.style;
      style.position = "absolute";
      style.top = "-5px";
      style.right = "0";
      style.height = "40px";
      style.cursor = "pointer";
    };
    const setIconSrc = (src: string) =>
      searchIcon.setAttribute("src", iconpaths.get(src));
    setIconStyle();
    inputcontainer.appendChild(searchIcon);
    const setInputStyle = () => {
      const style = input.style;
      style.border = "none";
      style.padding = "0.5rem";
      style.width = "70cqw";
      setBorderRadius("15px")(input);
      style.backgroundColor = "white";
      style.setProperty("--c", innerRed);
    };
    setInputStyle();
    container.appendChild(search);
    container.appendChild(inputcontainer);
    const setPlaceHolderColor = (color: string) =>
      input.style.setProperty("--c", placeholderColors.get(color));
    return [container, setPlaceHolderColor, inputValueCb, setIconSrc];
  };
  const createMain = () => {
    const container = createDiv();
    setHeight91cqh(container);
    container.id = "main";
    setDisplayGrid(container);
    container.style.gridTemplateRows = "1fr 1.4fr";
    return container;
  };
  const createSearchResult = (
    results: string[]
  ): [HTMLElement, eventCb[], colorCb] => {
    const container = createDiv();
    container.id = "results-area";
    container.style.overflow = "scroll";
    container.style.backgroundColor = innerRed;
    setBorderRadius("4px")(container);
    setMargin1rem(container);
    const createResult = (
      result: string,
      i: number
    ): [HTMLElement, eventCb] => {
      const container = createDiv();
      container.setAttribute("key", i.toString());
      container.style.backgroundColor = "white";
      const text = createP();
      setElementStyle(container, text);
      text.textContent = result;
      container.appendChild(text);
      const setSearchResultCb = (cb: (e: MouseEvent) => void) => {
        container.onclick = cb;
      };
      return [container, setSearchResultCb];
    };
    const resultsElements = results.map(createResult);
    resultsElements.forEach((e) => container.appendChild(e[0]));
    const setResultBackgroundColor = setElementBgColor(container);
    return [
      container,
      resultsElements.map((e) => e[1]),
      setResultBackgroundColor,
    ];
  };
  const createQueue = (
    currentqueue: string[]
  ): [HTMLElement, [eventCb[], eventCb[]], colorCb, colorCb, colorCb] => {
    const container = createDiv();
    container.style.overflow = "hidden";
    setDisplayGrid(container);
    container.id = "queue-area";
    const queueheader = createDiv();
    queueheader.id = "queue-header";
    setDisplayGrid(queueheader);
    queueheader.style.placeItems = "center";
    const createVote = () => {
      const vote = createP();
      vote.textContent = "VOTE";
      vote.style.color = innerRed;
      vote.style.fontSize = "0.8rem";
      return vote;
    };
    const voteL = createVote();
    const voteR = createVote();
    const setVoteColor = (color: string) => {
      [voteL, voteR].forEach((e) => setElementColor(e)(color));
    };
    const queue = createP();
    queue.textContent = "QUEUE";
    if (vote) {
      queueheader.style.gridTemplateColumns = gridTemplate;
      queueheader.style.margin = "0 1rem";
      queueheader.appendChild(voteL);
      queueheader.appendChild(queue);
      queueheader.appendChild(voteR);
    } else {
      queueheader.appendChild(queue);
    }
    const content = createDiv();
    setMargin1rem(content);
    content.id = "content-area";
    content.style.overflow = "scroll";
    content.style.backgroundColor = innerRed;
    const setQueueBackgroundColor = setElementBgColor(content);
    setBorderRadius("4px")(content);
    const createQueueElement = (
      infostext: string,
      i: number
    ): [HTMLElement] | [HTMLElement, eventCb, eventCb] => {
      const element = createDiv();
      const infos = createP();
      infos.style.backgroundColor = "white";
      infos.style.width = "-webkit-fill-available";
      setBorderRadius("4px")(infos);
      const margins = "2px";
      infos.style.marginLeft = margins;
      infos.style.marginRight = margins;
      element.id = "queue-el";
      element.setAttribute("key", i.toString());
      infos.textContent = infostext;
      setElementStyle(element, infos);
      if (vote) {
        setDisplayGrid(element);
        element.style.gridTemplateColumns = gridTemplate;
        const upButton = createButton(makeIconPath("upvote-arrow"));
        const setUpButtonCb = (cb: (e: MouseEvent) => void) => {
          upButton.onclick = cb;
        };
        const downButton = createButton(makeIconPath("downvote-arrow"));
        const setDownButtonCb = (cb: (e: MouseEvent) => void) => {
          downButton.onclick = cb;
        };
        element.appendChild(downButton);
        element.appendChild(infos);
        element.appendChild(upButton);
        infos.style.placeSelf = "center";
        return [element, setDownButtonCb, setUpButtonCb];
      } else {
        element.appendChild(infos);
        return [element];
      }
    };
    const tracksnumber = createP();
    const tracks = 25;
    tracksnumber.textContent = `(${tracks} TRACKS QUEUED)`;
    tracksnumber.style.placeSelf = "center";
    tracksnumber.style.color = innerRed;
    tracksnumber.style.fontSize = "0.8rem";
    const setTrackInfoColor = (color: string) =>
      setElementColor(tracksnumber)(color);
    content.style.marginTop = "0.2rem";
    content.style.marginBottom = "0.2rem";
    const queuelements = currentqueue.map(createQueueElement);
    queuelements.forEach((e) => content.appendChild(e[0]));
    container.appendChild(queueheader);
    container.appendChild(content);
    container.appendChild(tracksnumber);
    return [
      container,
      [queuelements.map((e) => e[1]!), queuelements.map((e) => e[2]!)],
      setQueueBackgroundColor,
      setVoteColor,
      setTrackInfoColor,
    ];
  };
  const createFooter = (icons: [string, string][]): [HTMLElement, colorCb] => {
    const container = createDiv();
    container.id = "footer-container";
    const subcontainer = createDiv();
    subcontainer.id = "footer-subcontainer";
    container.appendChild(subcontainer);
    setDisplayFlex(container);
    setDisplayGrid(subcontainer);
    const setContainerStyle = () => {
      const style = container.style;
      style.height = footerHeight + `cqh`;
      style.placeContent = "center";
      style.placeItems = "center";
    };
    const setSubContainerStyle = () => {
      const style = subcontainer.style;
      style.width = "90cqw";
      style.gridTemplateColumns = "1fr 1fr 1fr 1fr";
      style.placeSelf = "center";
      style.placeItems = "center";
    };
    setContainerStyle();
    setSubContainerStyle();
    const setColor = (color: string) =>
      (container.style.backgroundImage = gradientMap.get(color)[1]);
    const elements = icons.map((e) => {
      const a = document.createElement("a");
      const svg = createSvg(e[0], "24px");
      a.appendChild(svg);
      a.style.cursor = "pointer";
      a.setAttribute("href", e[1]);
      a.setAttribute("target", "_blank");
      return a;
    });
    elements.forEach((e, i, a) => {
      subcontainer.appendChild(e);
    });
    return [container, setColor];
  };

  const webui = createWebui();
  const [tabs, tabEvents] = createTabs();
  const [display, setDisplayColor] = createDisplay();
  const header = createHeader("NIRVANA - SMELLS LIKE TEEN SPIRIT");
  const [infos, setInfosOnClick, setInfosTextColor] = createInfos(
    "21 SATS PER TRACK & PER DOWN VOTE"
  );
  const [searchbar, setPlaceholderColor, inputValueCb, setIconSrc] =
    createSearchbar("red-search");
  const main = createMain();
  const [searchResults, searchResultCbSetters, setResultBackgroundColor] =
    createSearchResult(getResults());
  const [queue, votesCbs, setQueueBgColor, setVoteColor, setTracksInfoColor] =
    createQueue(getQueue());
  const [footer, setFooterColor] = createFooter([
    [makeIconPath("telegram-icon"), "urltelegram"],
    [
      makeIconPath("github-cion"),
      "https://github.com/LightningJukeboxBot/LightningJukeboxBot",
    ],
    [makeIconPath("x-icon"), "urlx"],
    [makeIconPath("nrfm-icon"), "urlnrfm"],
  ]);
  const activeTab = "red-tab";
  setMargin0(body);
  setDisplayColor(activeTab);
  setFooterColor(activeTab);
  setPlaceholderColor(activeTab);

  webui.appendChild(tabs);
  webui.appendChild(display);
  display.appendChild(header);
  display.appendChild(infos);
  display.appendChild(searchbar);
  display.appendChild(main);
  main.appendChild(searchResults);
  main.appendChild(queue);
  webui.appendChild(footer);
  const setActive = (tab: string) => {
    setDisplayColor(tab);
    setFooterColor(tab);
    setIconSrc(tab);
    setPlaceholderColor(tab);
    setInfosTextColor(tab);
    setResultBackgroundColor(tab);
    setQueueBgColor(tab);
    setVoteColor(tab);
    setTracksInfoColor(tab);
  };
  tabEvents.forEach((es) =>
    es((e) => {
      const element = e.target as HTMLElement;
      setActive(element.id);
    })
  );
  const searchResultCb = (cb: (e: MouseEvent) => void) =>
    searchResultCbSetters.forEach((e) => e(cb));
  const setVotesCb = (
    down: (e: MouseEvent) => void,
    up: (e: MouseEvent) => void
  ) => {
    const downs = votesCbs[0];
    const ups = votesCbs[1];
    downs.forEach((d) => d(down));
    ups.forEach((d) => d(up));
  };
  return [webui, setInfosOnClick, inputValueCb, searchResultCb, setVotesCb] as [
    HTMLElement,
    eventCb,
    (cb: (inputvalue: string) => void) => void,
    (cb: (e: MouseEvent) => void) => void,
    (down: (e: MouseEvent) => void, up: (e: MouseEvent) => void) => void
  ];
};

const body = document.body;
///// CLIENT
const [ui, setInfosOnClick, inputValueCb, searchResultCb, setVotesCb] =
  createUI();

body.prepend(ui);

// da portare in funzione

setInfosOnClick((e) => {
  console.log(e.target);
});

inputValueCb((e) => {
  console.log(e);
});

searchResultCb((e) => console.log(e.currentTarget));

setVotesCb(
  (e) => console.log(e.currentTarget),
  (e) => console.log(e.currentTarget)
);
