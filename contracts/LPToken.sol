// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract LPToken is ERC20 {
    address public pair;

    constructor(string memory name_, string memory symbol_)
        ERC20(name_, symbol_)
    {
        pair = msg.sender;
    }

    function mint(address to, uint256 amount) external {
        require(msg.sender == pair, "Only pair");
        _mint(to, amount);
    }

    function burn(address from, uint256 amount) external {
        require(msg.sender == pair, "Only pair");
        _burn(from, amount);
    }
}
