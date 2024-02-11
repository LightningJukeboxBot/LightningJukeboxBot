import {
  innerRed,
  bigSection,
  blueDark,
  blueLight,
  bluecolor,
  darkRed,
  fontMid,
  footerHeight,
  greenDark,
  greenLight,
  greencolor,
  gridTemplate,
  superDark,
  tabHeight,
  tabIconsSize,
  tabwidth,
  vote,
  telegramLink,
  githubLink,
  xLink,
  nrfmLink,
} from "./setup";
import {
  createButton,
  createDiv,
  createP,
  createSvg,
  makeIconPath,
  makeLinearGradient,
  openPopUp,
  setBorderRadius,
  setDisplayFlex,
  setDisplayGrid,
  setElementStyle,
  setHeight,
  setHeight6cqh,
  setHeight91cqh,
  setHeight9cqh,
  setMargin0,
  setMargin1rem,
} from "./utils";

export {};

const getResults = () => {
  return new Array(25).fill("EMPTY");
};

const getQueue = () => {
  return new Array(25).fill("I WILL BE THERE");
};

const createUI = (currentqueue: string[], results?: string[]) => {
  type eventCb = (cb: (e: MouseEvent) => void) => void;
  type colorCb = (color: string) => void;
  type trackCb = (track: string) => void;
  type textCb = (text: string) => void;
  type resultSetter = (res: string[]) => [HTMLElement, eventCb][];

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
  type container = {
    id: string;
    position: string;
    height: string;
  };
  type tab = {
    id: string;
    iconFilename: string;
    icon: {
      id: string;
    };
    bgColor: string;
    left?: string;
  };
  type inputContainerStyle = {
    position: string;
  };
  type inputStyle = {
    placeHolder: string;
    style: {
      border: string;
      padding: string;
      width: string;
      borderRadius: string;
      bgColor: string;
      property: {
        key: string;
        value: string;
      };
      maxHeight: string;
    };
  };
  type searchIcon = {
    id: string;
    style: {
      position: string;
      right: string;
      height: string;
      cursor: string;
      top: (top: string) => string;
    };
  };
  const createTabs = (settings: {
    tabSize: string;
    height: (active: boolean) => "100%" | "60%";
    width: string;
    position: string;
    bottom: string;
    borderRadius: string;
    display: string;
    placeItems: string;
    transition: string;
    container: container;
    redTab: tab;
    greenTab: tab;
    blueTab: tab;
  }): [HTMLElement, [eventCb, eventCb, eventCb], colorCb] => {
    const container = createDiv();
    container.id = settings.container.id;
    setDisplayFlex(container);
    const setContainerStyle = () => {
      const style = container.style;
      const { position, height } = settings.container;
      style.position = position;
      style.height = height;
    };
    setContainerStyle();
    // RED
    const red = createDiv();
    red.id = settings.redTab.id;
    const redsvg = createSvg(
      makeIconPath(settings.redTab.iconFilename),
      settings.tabSize
    );
    redsvg.id = settings.redTab.icon.id;
    red.appendChild(redsvg);
    red.style.backgroundColor = settings.redTab.bgColor;
    // GREEN
    const green = createDiv();
    green.id = settings.greenTab.id;
    const greenp = createP();
    const greensvg = createSvg(
      makeIconPath(settings.greenTab.iconFilename),
      settings.tabSize
    );
    green.appendChild(greensvg);
    greensvg.id = settings.greenTab.icon.id;
    const gr = greencolor;
    greenp.textContent = gr;
    green.style.backgroundColor = settings.greenTab.bgColor;
    green.style.left = settings.greenTab.left!;
    // BLUE
    const blue = createDiv();
    blue.id = settings.blueTab.id;
    const bluesvg = createSvg(
      makeIconPath(settings.blueTab.iconFilename),
      settings.tabSize
    );
    bluesvg.id = settings.blueTab.icon.id;
    blue.appendChild(bluesvg);
    blue.style.backgroundColor = settings.blueTab.bgColor;
    blue.style.left = settings.blueTab.left!;
    const setTabStyle = (tab: HTMLElement, active: boolean = true) => {
      const style = tab.style;
      style.width = settings.width;
      style.position = settings.position;
      style.bottom = settings.bottom;
      style.height = settings.height(active);
      style.borderRadius = settings.borderRadius;
      style.display = settings.display;
      style.placeItems = settings.placeItems;
      style.transition = settings.transition;
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
    const setTabsHeights = (color: string) => {
      [red, green, blue].forEach((e) => {
        if (e.id === color) {
          console.log(e.id);
          setTabStyle(e);
        } else {
          setTabStyle(e, false);
        }
      });
    };
    [red, green, blue].forEach((e) => setTabStyle(e));
    container.appendChild(red);
    container.appendChild(green);
    container.appendChild(blue);
    return [container, [setRedCb, setGreebCb, setBlueCb], setTabsHeights];
  };
  const createDisplay = (settings: {
    container: {
      id: string;
      height: string;
      containerType: string;
    };
  }): [HTMLElement, colorCb] => {
    const container = createDiv();
    container.id = "display-area";
    container.style.height = settings.container.height;
    container.style.containerType = "size";
    const setColor = (color: string) =>
      (container.style.backgroundImage = gradientMap.get(color));
    return [container, setColor];
  };
  const createHeader = (
    songinfostext: string,
    settings: {
      container: {
        id: string;
        placeItems: string;
        placeContent: string;
      };
      nowPlaying: {
        margintb: string;
        marginlr: string;
        borders: string;
      };
      makeText: (track: string) => string;
    }
  ): [HTMLElement, trackCb] => {
    const container = createDiv();
    container.style.placeItems = settings.container.placeItems;
    container.style.placeContent = settings.container.placeContent;
    container.id = settings.container.id;
    setHeight9cqh(container);
    const nowPlaying = createP();
    const makeText = settings.makeText;
    nowPlaying.textContent = makeText(songinfostext);
    const setNowPlayingStyle = () => {
      const style = nowPlaying.style;
      const margintb = settings.nowPlaying.margintb;
      const marginlr = settings.nowPlaying.marginlr;
      style.paddingTop = margintb;
      style.paddingRight = marginlr;
      style.paddingBottom = margintb;
      style.paddingLeft = marginlr;
      style.borderTop = settings.nowPlaying.borders;
      style.borderBottom = settings.nowPlaying.borders;
      style.fontSize = fontMid;
    };
    const setNowPlayingTrack: trackCb = (track) => {
      nowPlaying.textContent = makeText(track);
    };
    setNowPlayingStyle();
    setDisplayFlex(container);
    container.appendChild(nowPlaying);
    return [container, setNowPlayingTrack];
  };
  const createInfos = (
    text: string,
    settings: {
      container: {
        id: string;
        placeItems: string;
        placeContent: string;
      };
      iconFilename: string;
      icon: {
        cursor: string;
      };
      p: {
        fontSize: string;
        marginLeft: string;
        color: string;
      };
    }
  ): [HTMLElement, eventCb, colorCb, textCb] => {
    const container = createDiv();
    container.id = settings.container.id;
    container.style.placeItems = settings.container.placeItems;
    container.style.placeContent = settings.container.placeContent;
    setHeight6cqh(container);
    setDisplayFlex(container);
    const infosvg = createSvg(makeIconPath(settings.iconFilename));
    const infos = createP();
    infosvg.style.cursor = settings.icon.cursor;
    const setOnClick = (cb: (e: MouseEvent) => void) => (infosvg.onclick = cb);
    const setText = (text: string) => (infos.textContent = text);
    infos.textContent = setText(text);
    infos.style.fontSize = settings.p.fontSize;
    infos.style.marginLeft = settings.p.marginLeft;
    infos.style.color = settings.p.color;
    const setInfosTextColor = setElementColor(infos);
    container.appendChild(infosvg);
    container.appendChild(infos);
    return [container, setOnClick, setInfosTextColor, setText];
  };
  const createSearchbar = (
    iconFilename: string,
    settings: {
      container: {
        id: string;
        style: {
          placeItems: string;
          placeContent: string;
        };
      };
      search: {
        textContent: string;
        style: {
          marginRight: string;
          fontSize: string;
        };
      };
      inputContainer: {
        id: string;
        style: inputContainerStyle;
      };
      input: inputStyle;
      searchIcon: searchIcon;
    }
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
  const createMain = (settings: {
    container: {
      id: string;
      style: {
        gridTemplateRows: string;
      };
    };
  }) => {
    const container = createDiv();
    setHeight91cqh(container);
    container.id = settings.container.id;
    setDisplayGrid(container);
    container.style.gridTemplateRows =
      settings.container.style.gridTemplateRows;
    return container;
  };
  const createSearchResult = (settings: {
    container: {
      id: string;
      style: {
        overflow: string;
        bgColor: string;
        borderRadius: string;
      };
      attribute: {
        key: string;
      };
      result: {
        style: {
          backgroundColor: string;
        };
      };
    };
    results?: string[];
  }): [HTMLElement, eventCb[], colorCb, resultSetter] => {
    const container = createDiv();
    container.id = settings.container.id;
    container.style.overflow = settings.container.style.overflow;
    container.style.backgroundColor = settings.container.style.bgColor;
    setBorderRadius(settings.container.style.borderRadius)(container);
    setMargin1rem(container);
    const createResult = (
      result: string,
      i: number
    ): [HTMLElement, eventCb] => {
      const container = createDiv();
      container.setAttribute(settings.container.attribute.key, i.toString());
      container.style.backgroundColor =
        settings.container.result.style.backgroundColor;
      const text = createP();
      setElementStyle(container, text);
      text.textContent = result;
      container.appendChild(text);
      const setSearchResultCb = (cb: (e: MouseEvent) => void) => {
        container.onclick = cb;
      };
      return [container, setSearchResultCb];
    };
    const setResultBackgroundColor = setElementBgColor(container);
    const showResult = (res: string[]) => {
      const resultsElements = res.map(createResult);
      resultsElements.forEach((e) => container.appendChild(e[0]));
      return resultsElements;
    };
    if (results) {
      const resultsElements = showResult(results);
      return [
        container,
        resultsElements.map((e) => e[1]),
        setResultBackgroundColor,
        showResult,
      ];
    } else {
      return [container, [], setResultBackgroundColor, showResult];
    }
  };
  type queueT = {
    textContent: string;
  };
  type content = {
    id: string;
    style: {
      overflow: string;
      backgroundColor: string;
      borderRadius: string;
      marginTop: string;
      marginBottom: string;
    };
  };
  type infosT = {
    style: {
      backgroundColor: string;
      width: string;
      borderRadius: string;
      magins: string;
      placeSelf: string;
    };
    element: {
      id: string;
      attribute: {
        key: string;
      };
    };
    upvoteFilename: string;
    downvoteFilename: string;
  };
  type tracksNumber = {
    textContent: (tracks: string) => string;
    style: {
      placeSelf: string;
      color: string;
      fontSize: string;
    };
  };
  const createQueue = (
    currentqueue: string[],
    settings: {
      container: {
        style: {
          overflow: string;
        };
        id: string;
      };
      queueheader: {
        id: string;
        style: {
          placeItems: string;
          margin: string;
        };
      };
      vote: {
        textContent: string;
        style: {
          color: string;
          fontSize: string;
        };
      };
      queue: queueT;
      content: content;
      infos: infosT;
      tracksNumber: tracksNumber;
    }
  ): [HTMLElement, [eventCb[], eventCb[]], colorCb, colorCb, colorCb] => {
    const container = createDiv();
    container.style.overflow = settings.container.style.overflow;
    setDisplayGrid(container);
    container.id = settings.container.id;
    const queueheader = createDiv();
    queueheader.id = settings.queueheader.id;
    setDisplayGrid(queueheader);
    queueheader.style.placeItems = settings.queueheader.style.placeItems;
    const createVote = () => {
      const vote = createP();
      vote.textContent = settings.vote.textContent;
      vote.style.color = innerRed;
      vote.style.fontSize = settings.vote.style.fontSize;
      return vote;
    };
    const voteL = createVote();
    const voteR = createVote();
    const setVoteColor = (color: string) => {
      [voteL, voteR].forEach((e) => setElementColor(e)(color));
    };
    const queue = createP();
    queue.textContent = settings.queue.textContent;
    if (vote) {
      queueheader.style.gridTemplateColumns = gridTemplate;
      queueheader.style.margin = settings.queueheader.style.margin;
      queueheader.appendChild(voteL);
      queueheader.appendChild(queue);
      queueheader.appendChild(voteR);
    } else {
      queueheader.appendChild(queue);
    }
    const content = createDiv();
    setMargin1rem(content);
    content.id = settings.content.id;
    content.style.overflow = settings.content.style.overflow;
    content.style.backgroundColor = settings.content.style.backgroundColor;
    const setQueueBackgroundColor = setElementBgColor(content);
    setBorderRadius(settings.content.style.borderRadius)(content);
    const createQueueElement = (
      infostext: string,
      i: number
    ): [HTMLElement] | [HTMLElement, eventCb, eventCb] => {
      const element = createDiv();
      const infos = createP();
      infos.style.backgroundColor = settings.infos.style.backgroundColor;
      infos.style.width = settings.infos.style.width;
      setBorderRadius(settings.infos.style.borderRadius)(infos);
      const margins = settings.infos.style.magins;
      infos.style.marginLeft = margins;
      infos.style.marginRight = margins;
      element.id = settings.infos.element.id;
      element.setAttribute(settings.infos.element.attribute.key, i.toString());
      infos.textContent = infostext;
      setElementStyle(element, infos);
      if (vote) {
        setDisplayGrid(element);
        element.style.gridTemplateColumns = gridTemplate;
        const upButton = createButton(
          makeIconPath(settings.infos.upvoteFilename)
        );
        const setUpButtonCb = (cb: (e: MouseEvent) => void) => {
          upButton.onclick = cb;
        };
        const downButton = createButton(
          makeIconPath(settings.infos.downvoteFilename)
        );
        const setDownButtonCb = (cb: (e: MouseEvent) => void) => {
          downButton.onclick = cb;
        };
        element.appendChild(downButton);
        element.appendChild(infos);
        element.appendChild(upButton);
        infos.style.placeSelf = settings.infos.style.placeSelf;
        return [element, setDownButtonCb, setUpButtonCb];
      } else {
        element.appendChild(infos);
        return [element];
      }
    };
    const tracksnumber = createP();
    const tracks = 25;
    tracksnumber.textContent = settings.tracksNumber.textContent(
      tracks.toString()
    );
    tracksnumber.style.placeSelf = settings.tracksNumber.style.placeSelf;
    tracksnumber.style.color = innerRed;
    tracksnumber.style.fontSize = settings.tracksNumber.style.fontSize;
    const setTrackInfoColor = (color: string) =>
      setElementColor(tracksnumber)(color);
    content.style.marginTop = settings.content.style.marginTop;
    content.style.marginBottom = settings.content.style.marginBottom;
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
  type icons = {
    style: {
      height: string;
      cursor: string;
      attribute1: {
        key: string;
      };
      attribute2: {
        key: string;
        value: string;
      };
      placeSelf: string;
      palceItems: string;
    };
  };
  const createFooter = (
    icons: [string, string][],
    settings: {
      container: {
        id: string;
        subcontainer: {
          id: string;
          style: {
            gridTemplateColumns: string;
            placeSelf: string;
            placeItems: string;
            width: string;
          };
        };
        style: {
          height: string;
          placeContent: string;
          placeItems: string;
        };
      };
      icons: icons;
    }
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

  const webui = createWebui();
  const [tabs, tabEvents, setTabsHeights] = createTabs({
    tabSize: `${tabIconsSize}px`,
    height: (active: boolean) => (active ? "100%" : "60%"),
    width: tabwidth + `rem`,
    position: "absolute",
    bottom: "0",
    borderRadius: "5px 5px 0 0",
    display: "grid",
    placeItems: "center",
    transition: "height 0.5s ease",
    container: {
      id: "tab-area",
      position: "relative",
      height: tabHeight + "cqh",
    },
    redTab: {
      id: "red-tab",
      iconFilename: "Red-tab-icon",
      icon: { id: "red-tab-icon" },
      bgColor: darkRed,
    },
    greenTab: {
      id: "green-tab",
      iconFilename: "green-tab-icon",
      icon: { id: "green-tab-icon" },
      bgColor: greencolor,
      left: tabwidth + `rem`,
    },
    blueTab: {
      id: "blue-tab",
      iconFilename: "blue-tab-icon",
      icon: { id: "blue-tab-icon" },
      bgColor: bluecolor,
      left: tabwidth * 2 + `rem`,
    },
  });
  const [display, setDisplayColor] = createDisplay({
    container: {
      id: "display-area",
      height: 100 - [tabHeight, footerHeight].reduce((p, c) => p + c) + "cqh",
      containerType: "size",
    },
  });
  const [header, setNowPlayingTrack] = createHeader(
    "NIRVANA - SMELLS LIKE TEEN SPIRIT",
    {
      container: {
        id: "jb-header",
        placeItems: "center",
        placeContent: "space-evenly",
      },
      nowPlaying: {
        margintb: "0.3rem",
        marginlr: "0.2rem",
        borders: "solid 1px white",
      },
      makeText: (track: string) => `NOW PLAYING: ${track}`,
    }
  );
  const [infos, setInfosOnClick, setInfosTextColor, setText] = createInfos(
    "21 SATS PER TRACK & PER DOWN VOTE",
    {
      container: {
        id: "jukebox-infos",
        placeItems: "center",
        placeContent: "center",
      },
      iconFilename: "info",
      icon: { cursor: "pointer" },
      p: { fontSize: "0.8rem", marginLeft: "0.5rem", color: innerRed },
    }
  );
  const [searchbar, setPlaceholderColor, inputValueCb, setIconSrc] =
    createSearchbar("red-search", {
      container: {
        id: "search-bar",
        style: { placeItems: "center", placeContent: "center" },
      },
      search: {
        textContent: "SEARCH",
        style: { marginRight: "0.5rem", fontSize: fontMid },
      },
      inputContainer: { id: "search-input", style: { position: "relative" } },
      input: {
        placeHolder: "ARTIST TRACK + TITLE",
        style: {
          border: "none",
          padding: "0.5rem",
          width: "70cqw",
          borderRadius: "15px",
          bgColor: "white",
          property: { key: "--c", value: innerRed },
          maxHeight: "35px",
        },
      },
      searchIcon: {
        id: "searchIcon",
        style: {
          position: "absolute",
          right: "0",
          height: "40px",
          cursor: "pointer",
          top: (top: string) => `${top}px`,
        },
      },
    });
  const main = createMain({
    container: { id: "main", style: { gridTemplateRows: "1fr 1.4fr" } },
  });
  const [
    searchResults,
    searchResultCbSetters,
    setResultBackgroundColor,
    showResult,
  ] = createSearchResult({
    container: {
      id: "results-area",
      style: { overflow: "scroll", bgColor: innerRed, borderRadius: "4px" },
      attribute: { key: "key" },
      result: {
        style: { backgroundColor: "white" },
      },
    },
    results,
  });
  const [queue, votesCbs, setQueueBgColor, setVoteColor, setTracksInfoColor] =
    createQueue(currentqueue, {
      container: { style: { overflow: "hidden" }, id: "queue-area" },
      queueheader: {
        id: "queue-header",
        style: { placeItems: "center", margin: "0 1rem" },
      },
      vote: {
        textContent: "VOTE",
        style: { color: innerRed, fontSize: "0.8rem" },
      },
      queue: { textContent: "QUEUE" },
      content: {
        id: "content-area",
        style: {
          overflow: "scroll",
          backgroundColor: innerRed,
          borderRadius: "4px",
          marginTop: "0.2rem",
          marginBottom: "0.2rem",
        },
      },
      infos: {
        style: {
          backgroundColor: "white",
          width: "-webkit-fill-available",
          borderRadius: "4px",
          magins: "2px",
          placeSelf: "center",
        },
        element: {
          id: "queue-el",
          attribute: { key: "key" },
        },
        upvoteFilename: "upvote-arrow",
        downvoteFilename: "downvote-arrow",
      },
      tracksNumber: {
        textContent: (tracks: string) => `(${tracks} TRACKS QUEUED)`,
        style: { placeSelf: "center", color: innerRed, fontSize: "0.8rem" },
      },
    });
  const [footer, setFooterColor] = createFooter(
    [
      [makeIconPath("telegram-icon"), telegramLink],
      [makeIconPath("github-cion"), githubLink],
      [makeIconPath("x-icon"), xLink],
      [makeIconPath("nrfm-icon"), nrfmLink],
    ],
    {
      container: {
        id: "footer-container",
        subcontainer: {
          id: "footer-subcontainer",
          style: {
            gridTemplateColumns: "1fr 1fr 1fr 1fr",
            placeSelf: "center",
            placeItems: "center",
            width: "90cqw",
          },
        },
        style: {
          height: footerHeight + `cqh`,
          placeContent: "center",
          placeItems: "center",
        },
      },
      icons: {
        style: {
          height: "24px",
          cursor: "pointer",
          attribute1: { key: "href" },
          attribute2: { key: "target", value: "_blank" },
          placeSelf: "center",
          palceItems: "center",
        },
      },
    }
  );
  const activeTab = "red-tab";
  setMargin0(body);
  setDisplayColor(activeTab);
  setFooterColor(activeTab);
  setPlaceholderColor(activeTab);
  setTabsHeights(activeTab);

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
    setTabsHeights(tab);
    // console.log("cb");
  };
  tabEvents.forEach((es) =>
    es((e) => {
      const element = e.target as HTMLElement;
      setActive(element.id);
    })
  );
  const searchResultCb = (cb: (e: MouseEvent) => void) => {
    if (searchResultCbSetters.length !== 0)
      searchResultCbSetters.forEach((e) => e(cb));
  };
  const setVotesCb = (
    down: (e: MouseEvent) => void,
    up: (e: MouseEvent) => void
  ) => {
    const downs = votesCbs[0];
    const ups = votesCbs[1];
    downs.forEach((d) => d(down));
    ups.forEach((d) => d(up));
  };
  setInfosOnClick(async (e) => {
    // const test = await import("./popup");
    // console.log(test.ciao);

    openPopUp(
      "http://127.0.0.1:5500/web/popup.html",
      "_blank",
      "width=286, height=466"
    );
  });
  return [
    webui,
    inputValueCb,
    searchResultCb,
    setVotesCb,
    showResult,
    setNowPlayingTrack,
    setText,
  ] as [
    HTMLElement,
    (cb: (inputvalue: string) => void) => void,
    (cb: (e: MouseEvent) => void) => void,
    (down: (e: MouseEvent) => void, up: (e: MouseEvent) => void) => void,
    resultSetter,
    trackCb,
    textCb
  ];
};

const body = document.body;
///// CLIENT

const results = getResults();
const queue = getQueue();
const [
  ui,
  inputValueCb,
  searchResultCb,
  setVotesCb,
  showResult,
  setNowPlayingTrack,
  setText,
] = createUI(queue);

body.prepend(ui);

inputValueCb((e) => {
  console.log(e);
  showResult(results);
});

searchResultCb((e) => console.log(e.currentTarget));

setVotesCb(
  (e) => console.log(e.currentTarget),
  (e) => console.log(e.currentTarget)
);
setTimeout(() => setNowPlayingTrack("Gotek - Antistress"), 3000);
setTimeout(() => setText("now its fucking 420 sats per track!"), 6000);
