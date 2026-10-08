// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title CryptoAvenue - Decentralized Advertising Grid
 */
contract CryptoAvenue {
    address public owner;
    uint256 public constant TOTAL_PLOTS = 100;
    uint256 public plotPrice = 0.005 ether; // Domyślna cena za działkę

    struct Plot {
        address owner;
        string title;
        string imageURI;
        string targetUrl;
        uint256 pricePaid;
    }

    mapping(uint256 => Plot) public plots;

    event PlotPurchased(uint256 indexed plotId, address indexed owner, string title, string targetUrl);
    event PriceUpdated(uint256 newPrice);

    modifier onlyOwner() {
        require(msg.sender == owner, "Brak uprawnien");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    // Zakup wolnej działki
    function buyPlot(
        uint256 plotId, 
        string memory title, 
        string memory imageURI, 
        string memory targetUrl
    ) external payable {
        require(plotId < TOTAL_PLOTS, "Nieprawidlowe ID dzialki");
        require(plots[plotId].owner == address(0), "Dzialka jest juz zajeta");
        require(msg.value >= plotPrice, "Niewystarczajaca kwota ETH");

        plots[plotId] = Plot(msg.sender, title, imageURI, targetUrl, msg.value);

        emit PlotPurchased(plotId, msg.sender, title, targetUrl);
    }

    // Ustawienie nowej ceny przez właściciela platformy
    function setPlotPrice(uint256 _newPrice) external onlyOwner {
        plotPrice = _newPrice;
        emit PriceUpdated(_newPrice);
    }

    // Wypłata zebranych środków z reklam
    function withdraw() external onlyOwner {
        uint256 balance = address(this).balance;
        require(balance > 0, "Brak srodkow do wyplaty");
        payable(owner).transfer(balance);
    }

    // Pobranie danych o wszystkich działkach (zbiorczy odczyt dla UI)
    function getAllPlots() external view returns (Plot[] memory) {
        Plot[] memory allPlots = new Plot[](TOTAL_PLOTS);
        for (uint256 i = 0; i < TOTAL_PLOTS; i++) {
            allPlots[i] = plots[i];
        }
        return allPlots;
    }
}
