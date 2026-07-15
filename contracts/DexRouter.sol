// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./DexFactory.sol";
import "./DexPair.sol";

contract DexRouter {

    DexFactory public factory;

    constructor(address _factory) {
        factory = DexFactory(_factory);
    }

    function addLiquidity(
        address tokenA,
        address tokenB,
        uint256 amountA,
        uint256 amountB
    ) external {

        address pair =
            factory.getPair(
                tokenA,
                tokenB
            );

        require(
            pair != address(0),
            "PAIR_NOT_FOUND"
        );

        DexPair(pair).addLiquidity(
            amountA,
            amountB
        );
    }

    function swapExactToken0ForToken1(
        address pair,
        uint256 amountIn
    ) external {

        DexPair(pair)
            .swapToken0ForToken1(
                amountIn
            );
    }
}
